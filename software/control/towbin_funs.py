from datetime import datetime, date
from ome_types.model import OME, Image, Pixels, Channel
import tifffile
from control._def import *

def save_single_plane_tiff(image, saving_path,
                           compression='zlib',
                           compress_level=9, channels=None):
    
    dtype = image.dtype.name
    
    physical_size_x = CAMERA_PIXEL_SIZE_UM[CAMERA_SENSOR]
    physical_size_y = CAMERA_PIXEL_SIZE_UM[CAMERA_SENSOR]
    # "µm" doesn't work because of an encoding issue, but it is default anyway
    #physical_size_x_unit = 'micron \xB5'.encode('latin-1')
    #physical_size_y_unit = 'micron \xB5'.encode('latin-1')#"µm"

    # construct channel list
    size_z = 1
    if len(image.shape) > 2:
        size_z = image.shape[1]
    if channels is None:
        if len(image.shape) == 2:
            channels = [Channel(name='0')]
        else:
            channels = [Channel(name=str(i)) for i in range(image.shape[0])]
    else:
        channels = [Channel(name=c) for c in channels]
    
    # get current time
    iso_date_time = datetime.now().isoformat()
    
    # create OME metadata. Note that with ome_types, the dimensions order has to be 
    # of type XY--- e.g. XYCZT, XYTZC, etc. In case of multi-channel images, the
    # the actual numy array has to be of shape (C, X, Y) and not (X, Y, C)
    ome = OME()
    img = Image(
        acquisition_date=iso_date_time,

        pixels=Pixels(
            channels=channels,
            size_c=len(channels), size_t=1, size_x=image.shape[-1], size_y=image.shape[-2], size_z=size_z,
            type=dtype,
            dimension_order='XYCZT',
            physical_size_x=physical_size_x,
            # physical_size_x_unit=physical_size_x_unit,
            physical_size_y=physical_size_y,
            # physical_size_y_unit=physical_size_y_unit,
            metadata_only=True,
            )
    )   
    ome.images.append(img)
    
    tifffile.imwrite(saving_path, image, description=ome.to_xml(), metadata=None, imagej=False, contiguous=False,
                    compression=compression, compressionargs={'level': compress_level},
                    bigtiff=True)
    

def distance_moved(stage, coordinate):

    import numpy as np
    pos = stage.get_pos()

    distance = np.sqrt(np.sum((np.array([pos.x_mm, pos.y_mm]) - np.array(coordinate[0:2]))**2))
    return distance
