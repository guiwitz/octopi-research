"""Writing of z-stacks, movies and single images as (multipage) OME-TIFF, lossless or with lossy JPEG2000.

No hardware, no Qt. Needs `tifffile` (and `imagecodecs` for the lossy JPEG2000 option).

File layout: axes are always T, Z, C, Y, X (singleton axes are kept), pages in that order (C varies fastest).
The OME metadata holds one <Plane> per page: acquisition time (DeltaT, s since the start of the movie/stack), stage
position and exposure time. Lossless = zlib (deflate); lossy = JPEG2000 with a PSNR target ("quality", dB, relative to the
full scale of the data type) OR a target compression ratio ("ratio", default 100): then the quality is chosen automatically, just
before writing, so that the file is about `ratio` times smaller than the raw pixels (see calibrate_quality).
"""
import queue
import threading

import numpy as np

DEFAULT_LOSSY_RATIO = 100.0
RATIO_RANGE = (2.0, 2000.0)
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


def normalize_compression(compression, quality=None, ratio=None):
    """-> ('lossless', None, None) or ('lossy', quality_db_or_None, ratio_or_None); for lossy exactly one of the two is set
    (default ratio = DEFAULT_LOSSY_RATIO). Raises StackIOError for wrong values."""
    c = 'lossless' if compression is None else str(compression).strip().lower()
    if c in ('lossless', 'zlib', 'deflate', 'none'):
        if quality is not None or ratio is not None:
            raise StackIOError('quality and ratio only apply to compression="lossy"')
        return 'lossless', None, None
    if c in ('lossy', 'jpeg2000', 'jp2', 'j2k'):
        if quality is not None and ratio is not None:
            raise StackIOError('give quality (dB) OR ratio (target compression), not both')
        if quality is not None:
            q = float(quality)
            lo, hi = QUALITY_RANGE_DB
            if not (lo <= q <= hi):
                raise StackIOError(f'quality must be between {lo:g} and {hi:g} dB (PSNR; higher = better, bigger files), got {q:g}')
            return 'lossy', q, None
        r = DEFAULT_LOSSY_RATIO if ratio is None else float(ratio)
        lo, hi = RATIO_RANGE
        if not (lo <= r <= hi):
            raise StackIOError(f'ratio must be between {lo:g} and {hi:g} (file size = raw size / ratio), got {r:g}')
        return 'lossy', None, r
    raise StackIOError(f'compression must be "lossless" or "lossy", got {compression!r}')


def estimate_noise(img):
    """Pixel noise (standard deviation, counts) from the difference of neighbouring pixels (robust, MAD)."""
    a = np.asarray(img, dtype=np.float64)
    d = (a[:, 1:] - a[:, :-1]).ravel()
    if d.size == 0:
        return 0.0
    return float(1.4826 * np.median(np.abs(d - np.median(d))) / np.sqrt(2))


def _crop(img, n=1024):
    h, w = img.shape
    y0, x0 = max(0, (h - n) // 2), max(0, (w - n) // 2)
    return np.ascontiguousarray(img[y0:y0 + n, x0:x0 + n])


def calibrate_quality(data, ratio, tol_db=0.5):
    """Quality (dB) that gives about `ratio` x compression. data: (T, Z, C, Y, X). For every channel the middle plane is tested
    (central 1024 x 1024 crop, bisection on the quality); the LOWEST quality over the channels is used so that every channel reaches
    the ratio. Returns (quality_db, info) with info = per channel dict(quality_db, ratio, rmse, noise)."""
    import imagecodecs
    T, Z, C = data.shape[:3]
    lo_q, hi_q = QUALITY_RANGE_DB
    infos, chosen = [], []
    for c in range(C):
        img = _crop(np.asarray(data[T // 2, Z // 2, c]))
        raw = img.nbytes

        def enc(q):
            b = imagecodecs.jpeg2k_encode(img, level=float(q), reversible=False, numthreads=4)
            return raw / len(b), b
        lo, hi = lo_q, hi_q              # ratio(q) falls with q: find the largest q with ratio >= target
        if enc(lo)[0] < ratio:
            q = lo                       # cannot reach the ratio even at the lowest quality
        elif enc(hi)[0] >= ratio:
            q = hi
        else:
            while hi - lo > tol_db:
                mid = (lo + hi) / 2
                if enc(mid)[0] >= ratio:
                    lo = mid
                else:
                    hi = mid
            q = lo
        r, b = enc(q)
        rec = imagecodecs.jpeg2k_decode(b).astype(np.float64)
        infos.append(dict(channel_index=c, quality_db=round(q, 2), ratio=round(r, 1),
                          rmse=float(np.sqrt(np.mean((rec - img) ** 2))), noise=estimate_noise(img)))
        chosen.append(q)
    return float(min(chosen)), infos


def check_lossy_ok():
    ok, msg = lossy_available()
    if not ok:
        raise StackIOError(msg)


def _tiff_args(mode, quality):
    if mode == 'lossy':
        return dict(compression='jpeg2000', compressionargs={'level': float(quality), 'numthreads': 4})
    return dict(compression='zlib', compressionargs={'level': ZLIB_LEVEL})


def write_ome_tiff(path, data, channels, planes=None, pixel_size_um=None, z_step_um=None, time_increment_s=None,
                   compression='lossless', quality=None, ratio=None, description=None):
    """Write `data` (T, Z, C, Y, X) as a multipage OME-TIFF.

    channels: list of C names. planes: optional dict of lists with one entry per page in file order
    (keys: time_s, z_um, x_um, y_um, exposure_ms). description: free text stored as the image description (OME)."""
    import tifffile
    mode, q, ratio = normalize_compression(compression, quality, ratio)
    if mode == 'lossy':
        check_lossy_ok()
    data = np.asarray(data)
    if data.ndim != 5:
        raise StackIOError(f'data must have axes T,Z,C,Y,X, got shape {data.shape}')
    T, Z, C = data.shape[:3]
    if len(channels) != C:
        raise StackIOError(f'{len(channels)} channel names for {C} channels')
    n = T * Z * C
    info = dict(mode=mode, quality_db=q, target_ratio=ratio, raw_bytes=int(data.nbytes))
    if mode == 'lossy' and q is None:
        q, per_channel = calibrate_quality(data, ratio)
        info['channels'] = per_channel
        info['quality_db'] = q
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
    if description and mode == 'lossy':
        meta['Description'] = f'{description}; JPEG2000 quality {q:.1f} dB' + (f' (chosen for a {ratio:g}x target compression)' if ratio else '')
    tifffile.imwrite(path, data, ome=True, metadata=meta, bigtiff=True, **_tiff_args(mode, q))
    import os
    info['file'] = path
    info['file_bytes'] = os.path.getsize(path)
    info['ratio'] = info['raw_bytes'] / max(1, info['file_bytes'])
    return info


class AsyncWriter:
    """One background thread that encodes and writes files, so that acquisition does not wait for JPEG2000 encoding.
    A memory budget keeps the queue from growing without limit: submit() blocks while too many bytes are waiting."""

    def __init__(self, max_pending_bytes=2 * 1024 ** 3):
        self._q = queue.Queue()
        self._pending = 0
        self._max = max_pending_bytes
        self._cv = threading.Condition()
        self.errors = []
        self.results = []                           # what the write functions returned (dicts), collected for logging
        self._t = threading.Thread(target=self._run, name='jobs-writer', daemon=True)
        self._t.start()

    def _run(self):
        while True:
            item = self._q.get()
            if item is None:
                return
            fn, nbytes = item
            try:
                res = fn()
                if res is not None:
                    self.results.append(res)
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
