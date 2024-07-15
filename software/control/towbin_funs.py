from datetime import datetime, date
from ome_types.model import OME, Image, Pixels
import tifffile
from control._def import *

def save_single_plane_tiff(image, saving_path,
                           compression='zlib',
                           compress_level=8):
    
    dtype = image.dtype.name
    
    physical_size_x = CAMERA_PIXEL_SIZE_UM[CAMERA_SENSOR]
    physical_size_y = CAMERA_PIXEL_SIZE_UM[CAMERA_SENSOR]
    # "µm" doesn't work because of an encoding issue, but it is default anyway
    #physical_size_x_unit = 'micron \xB5'.encode('latin-1')
    #physical_size_y_unit = 'micron \xB5'.encode('latin-1')#"µm"

    iso_date_time = datetime.now().isoformat()
    
    ome = OME()
    img = Image(
        acquisition_date=iso_date_time,
        pixels=Pixels(
            size_c=1, size_t=1, size_x=image.shape[1], size_y=image.shape[0], size_z=1,
            type=dtype, dimension_order='XYZCT',
            physical_size_x=physical_size_x,
            # physical_size_x_unit=physical_size_x_unit,
            physical_size_y=physical_size_y,
            # physical_size_y_unit=physical_size_y_unit,
            metadata_only=True
        )
    )
    ome.images.append(img)
    tifffile.imwrite(saving_path, image, description=ome.to_xml(), metadata=None,
                        compression=compression, compressionargs={'level': compress_level},
                        bigtiff=True)
