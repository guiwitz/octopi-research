from datetime import datetime, date
from ome_types.model import OME, Image, Pixels, Channel
import numpy as np
import tifffile
from control._def import *

def _ome_xml(dtype_name, shape, channels=None):
    """OME-XML (metadata only) for an image or image stack of this shape: (Y, X) or (C, Z, Y, X)."""
    physical_size_x = CAMERA_PIXEL_SIZE_UM[CAMERA_SENSOR]
    physical_size_y = CAMERA_PIXEL_SIZE_UM[CAMERA_SENSOR]
    # "\u00b5m" doesn't work because of an encoding issue, but it is default anyway

    # construct channel list
    size_z = 1
    if len(shape) > 2:
        size_z = shape[1]
    if channels is None:
        if len(shape) == 2:
            channels = [Channel(name='0')]
        else:
            channels = [Channel(name=str(i)) for i in range(shape[0])]
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
            size_c=len(channels), size_t=1, size_x=shape[-1], size_y=shape[-2], size_z=size_z,
            type=dtype_name,
            dimension_order='XYCZT',
            physical_size_x=physical_size_x,
            physical_size_y=physical_size_y,
            metadata_only=True,
            )
    )
    ome.images.append(img)
    return ome.to_xml()


def save_single_plane_tiff(image, saving_path,
                           compression='zlib',
                           compress_level=9, channels=None):
    tifffile.imwrite(saving_path, image, description=_ome_xml(image.dtype.name, image.shape, channels), metadata=None, imagej=False, contiguous=False,
                    compression=compression, compressionargs={'level': compress_level},
                    bigtiff=True)


def merge_plane_tiffs(parts, saving_path, channels, numchannels, num_z, compression='zlib', compress_level=9, delete=True):
    """Merge single-plane TIFF files into ONE multichannel file with exactly the layout of save_single_plane_tiff(array (C, Z, Y, X)).
    parts: dict {(channel index, z index): path}. The pages are read and written one at a time (memory: one image), the result is written
    to a temporary name, checked (number of pages) and renamed; only then are the single files deleted (not if one of them is missing:
    that page is written as zeros and the existing files are kept). Returns (ok, list of missing (c, z)). Raises on errors, nothing is deleted then."""
    import os
    first = next((p for p in parts.values() if os.path.isfile(p)), None)
    if first is None:
        raise FileNotFoundError('merge: none of the single-plane files exists')
    ref = tifffile.imread(first)
    shape4 = (numchannels, num_z) + tuple(ref.shape[-2:])
    desc = _ome_xml(ref.dtype.name, shape4, channels)
    tmp = saving_path + '.part'
    missing = []
    try:
        with tifffile.TiffWriter(tmp, bigtiff=True, ome=False) as tw:
            for c in range(numchannels):
                for z in range(num_z):
                    p = parts.get((c, z))
                    if p is not None and os.path.isfile(p):
                        page = tifffile.imread(p).reshape(ref.shape[-2:])
                    else:
                        missing.append((c, z))
                        page = np.zeros(ref.shape[-2:], ref.dtype)
                    tw.write(page, photometric='minisblack', description=desc if (c == 0 and z == 0) else None, metadata=None, contiguous=False,
                             compression=compression, compressionargs={'level': compress_level})
        with tifffile.TiffFile(tmp) as tf:
            if len(tf.pages) != numchannels * num_z:
                raise RuntimeError(f'merge: {len(tf.pages)} pages written instead of {numchannels * num_z}')
        os.replace(tmp, saving_path)
    except BaseException:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise
    if delete and not missing:
        for p in parts.values():
            try:
                os.remove(p)
            except OSError:
                pass
    return (not missing), missing
