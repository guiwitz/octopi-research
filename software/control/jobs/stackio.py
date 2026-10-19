"""Writing of z-stacks, movies and single images as (multipage) OME-TIFF, lossless or with lossy JPEG2000.

No hardware, no Qt. Needs `tifffile` (and `imagecodecs` for the lossy JPEG2000 option).

File layout: axes are always T, Z, C, Y, X (singleton axes are kept), pages in that order (C varies fastest).
The OME metadata holds one <Plane> per page: acquisition time (DeltaT, s since the start of the movie/stack), stage
position and exposure time. Lossless = zlib (deflate); lossy = JPEG2000 with a PSNR target ("quality", dB, relative to the
full scale of the data type; ~80 dB for 16 bit is about 6x smaller than lossless with an error well below the photon noise).
"""
import queue
import threading

import numpy as np

DEFAULT_LOSSY_QUALITY_DB = 80.0
QUALITY_RANGE_DB = (40.0, 120.0)
ZLIB_LEVEL = 6


class StackIOError(Exception):
    pass


def lossy_available():
    """(ok, message): is JPEG2000 encoding possible here?"""
    try:
        import imagecodecs
        if not imagecodecs.JPEG2K.available:
            return False, 'imagecodecs is installed but has no JPEG2000 (OpenJPEG) support'
        return True, ''
    except Exception as e:
        return False, f'lossy JPEG2000 needs the Python package "imagecodecs" (pip install imagecodecs): {e}'


def normalize_compression(compression, quality):
    """-> ('lossless', None) or ('lossy', quality_db). Raises StackIOError for wrong values."""
    c = 'lossless' if compression is None else str(compression).strip().lower()
    if c in ('lossless', 'zlib', 'deflate', 'none'):
        if quality is not None:
            raise StackIOError('quality only applies to compression="lossy"')
        return 'lossless', None
    if c in ('lossy', 'jpeg2000', 'jp2', 'j2k'):
        q = DEFAULT_LOSSY_QUALITY_DB if quality is None else float(quality)
        lo, hi = QUALITY_RANGE_DB
        if not (lo <= q <= hi):
            raise StackIOError(f'quality must be between {lo:g} and {hi:g} dB (PSNR; higher = better, bigger files), got {q:g}')
        return 'lossy', q
    raise StackIOError(f'compression must be "lossless" or "lossy", got {compression!r}')


def check_lossy_ok():
    ok, msg = lossy_available()
    if not ok:
        raise StackIOError(msg)


def _tiff_args(mode, quality):
    if mode == 'lossy':
        return dict(compression='jpeg2000', compressionargs={'level': float(quality), 'numthreads': 4})
    return dict(compression='zlib', compressionargs={'level': ZLIB_LEVEL})


def write_ome_tiff(path, data, channels, planes=None, pixel_size_um=None, z_step_um=None, time_increment_s=None,
                   compression='lossless', quality=None, description=None):
    """Write `data` (T, Z, C, Y, X) as a multipage OME-TIFF.

    channels: list of C names. planes: optional dict of lists with one entry per page in file order
    (keys: time_s, z_um, x_um, y_um, exposure_ms). description: free text stored as the image description (OME)."""
    import tifffile
    mode, q = normalize_compression(compression, quality)
    if mode == 'lossy':
        check_lossy_ok()
    data = np.asarray(data)
    if data.ndim != 5:
        raise StackIOError(f'data must have axes T,Z,C,Y,X, got shape {data.shape}')
    T, Z, C = data.shape[:3]
    if len(channels) != C:
        raise StackIOError(f'{len(channels)} channel names for {C} channels')
    n = T * Z * C
    meta = dict(axes='TZCYX', Channel={'Name': [str(c) for c in channels]})
    if pixel_size_um:
        meta.update(PhysicalSizeX=float(pixel_size_um), PhysicalSizeY=float(pixel_size_um))
    if z_step_um:
        meta.update(PhysicalSizeZ=float(abs(z_step_um)))
    if time_increment_s:
        meta.update(TimeIncrement=float(time_increment_s), TimeIncrementUnit='s')
    if description:
        meta['Description'] = str(description)
    if planes:
        pl = {}
        if 'time_s' in planes:
            pl.update(DeltaT=[float(v) for v in planes['time_s']], DeltaTUnit=['s'] * n)
        if 'exposure_ms' in planes:
            pl.update(ExposureTime=[float(v) / 1000.0 for v in planes['exposure_ms']], ExposureTimeUnit=['s'] * n)
        for ax in 'xyz':
            if f'{ax}_um' in planes:
                pl[f'Position{ax.upper()}'] = [float(v) for v in planes[f'{ax}_um']]
                pl[f'Position{ax.upper()}Unit'] = ['µm'] * n
        for k, v in pl.items():
            if len(v) != n:
                raise StackIOError(f'plane metadata {k!r} has {len(v)} entries, expected {n}')
        meta['Plane'] = pl
    tifffile.imwrite(path, data, ome=True, metadata=meta, bigtiff=True, **_tiff_args(mode, q))


class AsyncWriter:
    """One background thread that encodes and writes files, so that acquisition does not wait for JPEG2000 encoding.
    A memory budget keeps the queue from growing without limit: submit() blocks while too many bytes are waiting."""

    def __init__(self, max_pending_bytes=2 * 1024 ** 3):
        self._q = queue.Queue()
        self._pending = 0
        self._max = max_pending_bytes
        self._cv = threading.Condition()
        self.errors = []
        self._t = threading.Thread(target=self._run, name='jobs-writer', daemon=True)
        self._t.start()

    def _run(self):
        while True:
            item = self._q.get()
            if item is None:
                return
            fn, nbytes = item
            try:
                fn()
            except Exception as e:                  # reported at flush(); the job continues
                self.errors.append(e)
            finally:
                with self._cv:
                    self._pending -= nbytes
                    self._cv.notify_all()

    def submit(self, fn, nbytes=0, should_abort=None):
        with self._cv:
            while self._pending > 0 and self._pending + nbytes > self._max:
                if should_abort is not None and should_abort():
                    break
                self._cv.wait(0.2)
            self._pending += nbytes
        self._q.put((fn, nbytes))

    @property
    def pending_bytes(self):
        return self._pending

    def flush(self):
        """Wait until everything is written. Returns the list of errors that happened (and clears it)."""
        done = threading.Event()
        self._q.put((done.set, 0))
        done.wait()
        errs, self.errors = self.errors, []
        return errs

    def close(self):
        errs = self.flush()
        self._q.put(None)
        self._t.join(timeout=5)
        return errs
