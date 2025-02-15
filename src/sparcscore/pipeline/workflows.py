from sparcscore.pipeline.segmentation import Segmentation, ShardedSegmentation, TimecourseSegmentation, MultithreadedSegmentation
from sparcscore.processing.preprocessing import percentile_normalization
from sparcscore.processing.utils import plot_image, visualize_class
from sparcscore.processing.segmentation import segment_local_threshold, segment_global_threshold, numba_mask_centroid, contact_filter, size_filter, _class_size, global_otsu

import os
import numpy as np
import torch
import matplotlib.pyplot as plt
import skfmm

from functools import partial
from multiprocessing import Pool


from skimage.filters import median
from skimage.morphology import binary_erosion, disk, dilation
from skimage.segmentation import watershed
from skimage.color import label2rgb

#for cellpose segmentation
from cellpose import models


class BaseSegmentation(Segmentation):

    def __init__(self, *args, **kwargs):
         super().__init__(*args, **kwargs)

    def _normalization(self, input_image):
        self.log("Started with normalized map")
        self.maps["normalized"] = percentile_normalization(input_image, 
                                                            self.config["lower_quantile_normalization"], 
                                                            self.config["upper_quantile_normalization"])
        self.save_map("normalized")
        self.log("Normalized map created")
    
    def _median_calculation(self):
        self.log("Started with median map")
        self.maps["median"] = np.copy(self.maps["normalized"])
                                
        for i, channel in enumerate(self.maps["median"]):
            self.maps["median"][i] = median(channel, disk(self.config["median_filter_size"]))
        
        self.save_map("median")
        self.log("Median map created")
    
    def _nucleus_thresholding(self):
        self.log("Generating thresholded nucleus map.")
        
        nucleus_map_tr = percentile_normalization(self.maps["median"][0],
                                                    self.config["nucleus_segmentation"]["lower_quantile_normalization"],
                                                    self.config["nucleus_segmentation"]["upper_quantile_normalization"])

        # Use manual threshold if defined in ["wga_segmentation"]["threshold"]
        # If not, use global otsu
        if 'threshold' in self.config["nucleus_segmentation"] and 'median_block' in self.config["nucleus_segmentation"]:
            self.maps["nucleus_segmentation"] = segment_local_threshold(nucleus_map_tr, 
                                        dilation=self.config["nucleus_segmentation"]["dilation"], 
                                        thr=self.config["nucleus_segmentation"]["threshold"], 
                                        median_block=self.config["nucleus_segmentation"]["median_block"], 
                                        min_distance=self.config["nucleus_segmentation"]["min_distance"], 
                                        peak_footprint=self.config["nucleus_segmentation"]["peak_footprint"], 
                                        speckle_kernel=self.config["nucleus_segmentation"]["speckle_kernel"], 
                                        median_step=self.config["nucleus_segmentation"]["median_step"],
                                        debug=self.debug)
        else:
            self.log('No treshold or median_block for nucleus segmentation defined, global otsu will be used.')
            self.maps["nucleus_segmentation"] = segment_global_threshold(nucleus_map_tr, 
                                        dilation=self.config["nucleus_segmentation"]["dilation"], 
                                        min_distance=self.config["nucleus_segmentation"]["min_distance"], 
                                        peak_footprint=self.config["nucleus_segmentation"]["peak_footprint"], 
                                        speckle_kernel=self.config["nucleus_segmentation"]["speckle_kernel"], 
                                        debug=self.debug)            
        
        del nucleus_map_tr
        self.save_map("nucleus_segmentation")
        self.log("Thresholded nucleus map created.")

    def _nucleus_mask_generation(self):
        self.log("Started with nucleus mask map")
        self.maps["nucleus_mask"] = np.clip(self.maps["nucleus_segmentation"], 0,1)
        self.save_map("nucleus_mask")
        self.log("Nucleus mask map created with {} elements".format(np.max(self.maps["nucleus_segmentation"])))

    def _filter_nuclei_classes(self):
        
        # filter nuclei based on size and contact
        center_nuclei, length = _class_size(self.maps["nucleus_segmentation"], debug=self.debug)
        all_classes = np.unique(self.maps["nucleus_segmentation"])

        # ids of all nucleis which are unconnected and can be used for further analysis
        labels_nuclei_unconnected = contact_filter(self.maps["nucleus_segmentation"], 
                                    threshold=self.config["nucleus_segmentation"]["contact_filter"], 
                                    reindex=False)
        classes_nuclei_unconnected = np.unique(labels_nuclei_unconnected)

        self.log("Filtered out due to contact limit: {} ".format(len(all_classes)-len(classes_nuclei_unconnected)))

        labels_nuclei_filtered = size_filter(self.maps["nucleus_segmentation"],
                                             limits=[self.config["nucleus_segmentation"]["min_size"],
                                                     self.config["nucleus_segmentation"]["max_size"]])
        
        
        classes_nuclei_filtered = np.unique(labels_nuclei_filtered)
        
        self.log("Filtered out due to size limit: {} ".format(len(all_classes)-len(classes_nuclei_filtered)))


        filtered_classes = set(classes_nuclei_unconnected).intersection(set(classes_nuclei_filtered))
        self.log("Filtered out: {} ".format(len(all_classes)-len(filtered_classes)))
        
        if self.debug:
            self._plot_nucleus_size_distribution(length)
            self._visualize_nucleus_segmentation(classes_nuclei_unconnected, classes_nuclei_filtered)

        return(all_classes, filtered_classes, center_nuclei)
    
    def _cellmembrane_mask_calculation(self):
        self.log("Started with WGA mask map")
        
        if "wga_background_image" in self.config["wga_segmentation"]:
                if self.config["wga_segmentation"]["wga_background_image"]:
                    # Perform percentile normalization
                    wga_mask_comp = percentile_normalization(self.maps["median"][-1],
                                                                self.config["wga_segmentation"]["lower_quantile_normalization"],
                                                                self.config["wga_segmentation"]["upper_quantile_normalization"])
                else:
                    # Perform percentile normalization
                    wga_mask_comp = percentile_normalization(self.maps["median"][1],
                                                                self.config["wga_segmentation"]["lower_quantile_normalization"],
                                                                self.config["wga_segmentation"]["upper_quantile_normalization"])
        else:
            # Perform percentile normalization
            wga_mask_comp = percentile_normalization(self.maps["median"][1],
                                                        self.config["wga_segmentation"]["lower_quantile_normalization"],
                                                        self.config["wga_segmentation"]["upper_quantile_normalization"])
            
        
        # Use manual threshold if defined in ["wga_segmentation"]["threshold"]
        # If not, use global otsu
        if 'threshold' in self.config["wga_segmentation"]:
            wga_mask = wga_mask_comp < self.config["wga_segmentation"]["threshold"]
        else:
            self.log('No treshold for cytosol segmentation defined, global otsu will be used.')
            wga_mask = wga_mask_comp < global_otsu(wga_mask_comp)


        wga_mask = wga_mask.astype(float)
        wga_mask -= self.maps["nucleus_mask"]
        wga_mask = np.clip(wga_mask,0,1)

        # Apply dilation and erosion
        wga_mask = dilation(wga_mask, footprint=disk(self.config["wga_segmentation"]["erosion"]))
        self.maps["wga_mask"] = binary_erosion(wga_mask, footprint=disk(self.config["wga_segmentation"]["dilation"]))
        
        self.save_map("wga_mask")
        self.log("WGA mask map created")
    
    def _cellmembrane_potential_mask(self):
        self.log("Started with WGA potential map")
        
        wga_mask_comp  = self.maps["median"][1] - np.quantile(self.maps["median"][1],0.02)

        nn = np.quantile(self.maps["median"][1],0.98)
        wga_mask_comp = wga_mask_comp / nn
        wga_mask_comp = np.clip(wga_mask_comp, 0, 1)
        
        # substract golgi and dapi channel from wga
        diff = np.clip(wga_mask_comp-self.maps["median"][0],0,1)
        diff = np.clip(diff-self.maps["nucleus_mask"],0,1)
        diff = 1-diff

        # enhance WGA map to generate speedmap
        # WGA 0.7-0.9
        min_clip = self.config["wga_segmentation"]["min_clip"]
        max_clip = self.config["wga_segmentation"]["max_clip"]
        diff = (np.clip(diff,min_clip,max_clip)-min_clip)/(max_clip-min_clip)

        diff = diff*0.9+0.1
        diff = diff.astype(dtype=float)

        self.maps["wga_potential"] = diff
        
        self.save_map("wga_potential")
        self.log("WGA mask potential created")
    
    def _cellmembrane_fastmarching(self, center_nuclei):
        self.log("Started with fast marching")
        fmm_marker = np.ones_like(self.maps["median"][0])
        px_center = np.round(center_nuclei).astype(int)
        
        for center in px_center[1:]:
            fmm_marker[center[0],center[1]] = 0
            
        fmm_marker  = np.ma.MaskedArray(fmm_marker, self.maps["wga_mask"])
        travel_time = skfmm.travel_time(fmm_marker, self.maps["wga_potential"])

        if not isinstance(travel_time, np.ma.core.MaskedArray):
            raise TypeError("travel_time for WGA based segmentation returned no MaskedArray. This is most likely due to missing WGA background determination.")
            
        self.maps["travel_time"] = travel_time.filled(fill_value=np.max(travel_time))
            
        self.save_map("travel_time")
        self.log("Fast marching finished")

    def _cellmembrane_watershed(self, center_nuclei):

        self.log("Started with watershed")   
        
        marker = np.zeros_like(self.maps["median"][1])
        
        px_center = np.round(center_nuclei).astype(int)
        for i, center in enumerate(px_center[1:]):
            marker[center[0],center[1]] = i+1

        wga_labels = watershed(self.maps["travel_time"], marker, mask=self.maps["wga_mask"]==0)
        self.maps["watershed"] = np.where(self.maps["wga_mask"]> 0.5,0,wga_labels)
        
        if self.debug:
            self._visualize_watershed_results(center_nuclei)

        self.save_map("watershed")
        self.log("watershed finished")

    def _filter_cells_cytosol_size(self, all_classes, filtered_classes):
        # filter cells based on cytosol size
        center_cell, length, coords = numba_mask_centroid(self.maps["watershed"], debug=self.debug)
        
        all_classes_wga = np.unique(self.maps["watershed"])

        labels_wga_filtered = size_filter(self.maps["watershed"],
                                                limits=[self.config["wga_segmentation"]["min_size"],
                                                        self.config["wga_segmentation"]["max_size"]])
        
        classes_wga_filtered = np.unique(labels_wga_filtered)
        
        self.log("Cells filtered out due to cytosol size limit: {} ".format(len(all_classes_wga)-len(classes_wga_filtered)))

        filtered_classes_wga = set(classes_wga_filtered)
        filtered_classes = set(filtered_classes).intersection(filtered_classes_wga)
        self.log("Filtered out: {} ".format(len(all_classes)-len(filtered_classes)))
        self.log("Remaining: {} ".format(len(filtered_classes)))
        
        if self.debug:
            self._plot_cytosol_size_distribution(length)
            self._visualize_cytosol_filtering(classes_wga_filtered)

        return(filtered_classes)

    #functions to generate quality control plots
    def _dapi_median_intensity_plot(self):
        #generate plot of dapi median intensity
        plt.hist(self.maps["median"][0].flatten(),bins=100,log=False)
        plt.xlabel("intensity")
        plt.ylabel("frequency")
        plt.yscale('log')
        plt.title("DAPI intensity distribution")
        plt.savefig("dapi_intensity_dist.png")
        plt.show()
    
    def _cellmembrane_median_intensity_plot(self):
        #generate plot of median Cellmembrane Marker intensity
        plt.hist(self.maps["median"][1].flatten(),bins=100,log=False)
        plt.xlabel("intensity")
        plt.ylabel("frequency")
        plt.yscale('log')
        plt.title("WGA intensity distribution")
        plt.savefig("wga_intensity_dist.png")
        plt.show()

    def _visualize_nucleus_segmentation(self, classes_nuclei_unconnected, classes_nuclei_filtered):
        um_p_px = 665 / 1024  #what is this!!?? @GWallmann
        um_2_px = um_p_px*um_p_px  #what is this!!?? @GWallmann
        
        visualize_class(classes_nuclei_unconnected, self.maps["nucleus_segmentation"], self.maps["normalized"][0])
        visualize_class(classes_nuclei_filtered, self.maps["nucleus_segmentation"], self.maps["normalized"][0])
    
    def _plot_nucleus_size_distribution(self, length):
        plt.hist(length,bins=50)
        plt.xlabel("px area")
        plt.ylabel("number")
        plt.title('Nucleus size distribution')
        plt.savefig('nucleus_size_dist.png')
        plt.show()

    def _plot_cytosol_size_distribution(self, length):
        plt.hist(length, bins=50)
        plt.xlabel("px area")
        plt.ylabel("number")
        plt.title('Cytosol size distribution')
        plt.savefig('cytosol_size_dist.png')
        plt.show()

    def _visualize_watershed_results(self, center_nuclei):
        image = label2rgb(self.maps["watershed"] ,self.maps["normalized"][0], bg_label=0, alpha=0.2)

        fig = plt.figure(frameon=False)
        fig.set_size_inches(10,10)
        ax = plt.Axes(fig, [0., 0., 1., 1.])
        ax.set_axis_off()
        fig.add_axes(ax)
        ax.imshow(image)
        plt.scatter(center_nuclei[:,1],center_nuclei[:,0],color="red")
        plt.savefig(os.path.join(self.directory, "watershed.png"))
        plt.show()
    
    def _visualize_cytosol_filtering(self, classes_wga_filtered):
        um_p_px = 665 / 1024 #what is this!!?? @GWallmann
        um_2_px = um_p_px*um_p_px #what is this!!?? @GWallmann

        visualize_class(classes_wga_filtered, self.maps["watershed"], self.maps["normalized"][1])
        visualize_class(classes_wga_filtered, self.maps["watershed"], self.maps["normalized"][0])

class WGASegmentation(BaseSegmentation):
    
    def __init__(self, *args, **kwargs):
         super().__init__(*args, **kwargs)

    def _finalize_segmentation_results(self):
        # The required maps are the nucelus channel and a membrane marker channel like WGA
        required_maps = [self.maps["normalized"][0],
                         self.maps["normalized"][1]]
        
        # Feature maps are all further channel which contain phenotypes needed for the classification
        if "wga_background_image" in self.config["wga_segmentation"]:
            if self.config["wga_segmentation"]["wga_background_image"]:
                #remove last channel since this is a pseudo channel to perform the WGA background calculation on
                feature_maps = [element for element in self.maps["normalized"][2:-1]]
            else:
                feature_maps = [element for element in self.maps["normalized"][2:]]
        else:   
            feature_maps = [element for element in self.maps["normalized"][2:]]
            
        channels = np.stack(required_maps + feature_maps).astype("float64")                   
        
        segmentation = np.stack([self.maps["nucleus_segmentation"],
                                self.maps["watershed"]]).astype("int32")

        return(channels, segmentation)
    
    def process(self, input_image):
        
        #self.directory = super().get_directory(super())
        
        self.maps = {"normalized": None,
                     "median": None,
                     "nucleus_segmentation": None,
                     "nucleus_mask": None,
                     "wga_mask":None,
                     "wga_potential": None,
                     "travel_time":None,
                     "watershed":None}
        
        start_from = self.load_maps_from_disk()
        
        if self.identifier is not None:
            self.log(f"Segmentation started shard {self.identifier}, starting from checkpoint {start_from}")
            
        else:
            self.log(f"Segmentation started, starting from checkpoint {start_from}")
        
        # Normalization
        if start_from <= 0:
            self._normalization(input_image)
             
        # Median calculation
        if start_from <= 1:
            self._median_calculation()

            if self.debug:
                self._dapi_median_intensity_plot()
                self._cellmembrane_median_intensity_plot()
        
        # segment dapi channels based on local tresholding  
        if start_from <= 2:
            self.log("Started performing nucleus segmentation.")
            self._nucleus_thresholding()
        
        # Calc nucleus map
        if start_from <= 3:
            self._nucleus_mask_generation()
        
        all_classes, filtered_classes, center_nuclei = self._filter_nuclei_classes()
        
        # create background map based on WGA
        if start_from <= 4:
            self._cellmembrane_mask_calculation()
            
        # create WGA potential map
        if start_from <= 5:
            self._cellmembrane_potential_mask()
        
        # WGA cytosol segmentation by fast marching
        
        if start_from <= 6:
            self._cellmembrane_fastmarching(center_nuclei)
                
        if start_from <= 7:
            self._cellmembrane_watershed(center_nuclei)
        
        filtered_classes = self._filter_cells_cytosol_size(all_classes, filtered_classes)   
        channels, segmentation = self._finalize_segmentation_results()

        results = self.save_segmentation(channels, segmentation, filtered_classes)
        
        #self.save_segmentation_zarr(channels, segmentation) #currently save both since we have not fully converted.
        return(results)
    
class ShardedWGASegmentation(ShardedSegmentation):

    method = WGASegmentation
    def __init__(self, *args, **kwargs):
         super().__init__(*args, **kwargs)

class DAPISegmentation(BaseSegmentation):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def _finalize_segmentation_results(self):
        # The required maps are only nucleus channel
        required_maps = [self.maps["normalized"][0]]
        
        # Feature maps are all further channel which contain phenotypes needed for the classification
        feature_maps = [element for element in self.maps["normalized"][1:]]
            
        channels = np.stack(required_maps+feature_maps).astype("float64")
                    
        segmentation = np.stack([self.maps["nucleus_segmentation"]]).astype("int32")
        return(channels, segmentation)
    
    def process(self, input_image):
        self.maps = {"normalized": None,
                     "median": None,
                     "nucleus_segmentation": None,
                     "nucleus_mask": None,
                     "travel_time":None}
        
        start_from = self.load_maps_from_disk()
        
        if self.identifier is not None:
            self.log(f"Segmentation started shard {self.identifier}, starting from checkpoint {start_from}")
            
        else:
            self.log(f"Segmentation started, starting from checkpoint {start_from}")
        
        # Normalization
        if start_from <= 0:
            self._normalization(input_image)
             
        # Median calculation
        if start_from <= 1:
            self._median_calculation()

            if self.debug:
                self._dapi_median_intensity_plot()
        
        # segment dapi channels based on local tresholding  
        if start_from <= 2:
            self.log("Started performing nucleus segmentation.")
            self._nucleus_thresholding()
        
        # Calc nucleus map
        if start_from <= 3:
            self._nucleus_mask_generation()
        
        _, filtered_classes, _ = self._filter_nuclei_classes() 
        channels, segmentation = self._finalize_segmentation_results()

        self.save_segmentation(channels, segmentation, filtered_classes)
        #self.save_segmentation_zarr(channels, segmentation) #currently save both since we have not fully converted.

class ShardedDAPISegmentation(ShardedSegmentation): 
    method = DAPISegmentation

    # def __init__(self, *args, **kwargs):
    #     super().__init__(*args, **kwargs)

class DAPISegmentationCellpose(BaseSegmentation):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def _finalize_segmentation_results(self):
        # The required maps are only nucleus channel
        required_maps = [self.maps["normalized"][0]]
        
        # Feature maps are all further channel which contain phenotypes needed for the classification
        if self.maps["normalized"].shape[0] > 1:
            feature_maps = [element for element in self.maps["normalized"][1:]]
            channels = np.stack(required_maps+feature_maps).astype("float64")
        else:
            channels = np.stack(required_maps).astype("float64")
                    
        segmentation = np.stack([self.maps["nucleus_segmentation"]]).astype("uint32")
        return(channels, segmentation)

    def cellpose_segmentation(self, input_image):
        #check that image is int
        input_image = input_image.astype('int')

        #check if GPU is available
        if torch.cuda.is_available():
            use_GPU = True
        else:
            use_GPU = False
        # use_GPU = core.use_gpu() currently no realy acceleration through using GPU as we can't load batches, so force run on CPU

        #load correct segmentation model
        model = models.Cellpose(model_type='nuclei', gpu = use_GPU)
        masks, _, _, _ = model.eval([input_image], diameter = None, channels = [1,0]) 
        masks = np.array(masks) #convert to array

        self.log(f"Segmented mask shape: {masks.shape}")
        self.maps["nucleus_segmentation"] = masks.reshape(masks.shape[1:]) #need to add reshape so that hopefully saving works out

    
    def process(self, input_image):

        #initialize location to save masks to
        self.maps = {"normalized":None,
                     "nucleus_segmentation": None}

        #could add a normalization step here if so desired
        self.maps["normalized"] = input_image

        self.log("Starting Cellpose DAPI Segmentation.")
        self.cellpose_segmentation(input_image)
        
        #currently no implemented filtering steps to remove nuclei outside of specific thresholds
        all_classes = np.unique(self.maps["nucleus_segmentation"])

        channels, segmentation = self._finalize_segmentation_results()
        
        results = self.save_segmentation(channels, segmentation, all_classes)
        return(results)

class ShardedDAPISegmentationCellpose(ShardedSegmentation): 
    method = DAPISegmentationCellpose

    # def __init__(self, *args, **kwargs):
    #     super().__init__(*args, **kwargs)

class CytosolSegmentationCellpose(BaseSegmentation):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def _finalize_segmentation_results(self):
        # The required maps are only nucleus channel
        required_maps = [self.maps["normalized"][0], self.maps["normalized"][1]]
        
        # Feature maps are all further channel which contain phenotypes needed for the classification
        if self.maps["normalized"].shape[0] > 2:
            feature_maps = [element for element in self.maps["normalized"][2:]]
            channels = np.stack(required_maps+feature_maps).astype("float64")
        else:
            channels = np.stack(required_maps).astype("float64")
                    
        segmentation = np.stack([self.maps["nucleus_segmentation"], self.maps["cytosol_segmentation"]]).astype("int32")
        return(channels, segmentation)

    def cellpose_segmentation(self, input_image):
        #torch.cuda.empty_cache()
        #with torch.no_grad():  
        
        #check that image is int
        input_image = input_image.astype('int')

        #check if GPU is available
        
        #use_GPU = False
        use_GPU = True #currently no real acceleration through using GPU as we can't load batches, so force run on CPU
        self.log(f"GPU Status for segmentation: {use_GPU}")
        
        #load correct segmentation model for nuclei
        model_name = self.config["nucleus_segmentation"]["model"]
        self.log(f"Segmenting nuclei using the following model: {model_name}")
        model = models.Cellpose(model_type=self.config["nucleus_segmentation"]["model"], gpu = use_GPU)
        masks, _, _, _ = model.eval([input_image], diameter = None, channels = [1, 0]) 
        masks = np.array(masks) #convert to array
        self.maps["nucleus_segmentation"] = masks.reshape(masks.shape[1:]) #need to add reshape so that hopefully saving works out

        model_name = self.config["cytosol_segmentation"]["model"]
        self.log(f"Segmenting cytosol using the following model: {model_name}")
        model = models.Cellpose(model_type=self.config["cytosol_segmentation"]["model"], gpu = use_GPU)
        masks, _, _, _ = model.eval([input_image], diameter = None, channels = [2, 1]) 
        masks = np.array(masks) #convert to array

        self.maps["cytosol_segmentation"] = masks.reshape(masks.shape[1:]) #need to add reshape so that hopefully saving works out
    
    def process(self, input_image):

        #initialize location to save masks to
        self.maps = {"normalized":None,
                     "nucleus_segmentation": None,
                     "cytosol_segmentation": None}

        #could add a normalization step here if so desired
        self.maps["normalized"] = input_image

        #self.log("Starting Cellpose DAPI Segmentation.")
        self.cellpose_segmentation(input_image)
        
        #currently no implemented filtering steps to remove nuclei outside of specific thresholds
        all_classes = np.unique(self.maps["nucleus_segmentation"])
        print(all_classes)

        channels, segmentation = self._finalize_segmentation_results()
        results = self.save_segmentation(channels, segmentation, all_classes)
        return(results)

class WGATimecourseSegmentation(TimecourseSegmentation):
    """
    Specialized Processing for Timecourse segmentation (i.e. smaller tiles not stitched together from many different wells and or timepoints).
    No intermediate results are saved and everything is written to one .hdf5 file.
    """
    class WGASegmentation_Timecourse(WGASegmentation, TimecourseSegmentation):
        method = WGASegmentation

    method = WGASegmentation_Timecourse

    def __init__(self, *args, **kwargs):
         super().__init__(*args, **kwargs)

    
class MultithreadedWGATimecourseSegmentation(MultithreadedSegmentation):
    class WGASegmentation_Timecourse(WGASegmentation, TimecourseSegmentation):
        method = WGASegmentation

    method = WGASegmentation_Timecourse

    def __init__(self, *args, **kwargs):
         super().__init__(*args, **kwargs) 

class CytosolCellposeTimecourseSegmentation(TimecourseSegmentation):
    """
    Specialized Processing for Timecourse segmentation (i.e. smaller tiles not stitched together from many different wells and or timepoints).
    No intermediate results are saved and everything is written to one .hdf5 file. Uses Cellpose segmentation models.
    """
    class CytosolSegmentationCellpose_Timecourse(CytosolSegmentationCellpose, TimecourseSegmentation):
        method = CytosolSegmentationCellpose

    method = CytosolSegmentationCellpose_Timecourse

    def __init__(self, *args, **kwargs):
         super().__init__(*args, **kwargs)

class MultithreadedCytosolCellposeTimecourseSegmentation(MultithreadedSegmentation):
    class CytosolSegmentationCellpose_Timecourse(CytosolSegmentationCellpose, TimecourseSegmentation):
        method = CytosolSegmentationCellpose

    method = CytosolSegmentationCellpose_Timecourse

    def __init__(self, *args, **kwargs):
         super().__init__(*args, **kwargs) 