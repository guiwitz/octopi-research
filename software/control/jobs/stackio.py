"""Writing of z-stacks, movies and single images as (multipage) OME-TIFF, lossless or with lossy JPEG2000.

No hardware, no Qt. Needs `tifffile` (and `imagecodecs` for the lossy JPEG2000 option).

File layout: axes are always T, Z, C, Y, X (singleton axes are kept), pages in that order (C varies fastest).
The OME metadata holds one <Plane> per page: acquisition time (DeltaT, s since the start of the movie/stack), stage
position and exposure time. Lossless = zlib (deflate); lossy = JPEG2000 with a PSNR target ("quality", dB, relative to the
full scale of the data type), a target compression ratio ("ratio", a number, or a dict {channel: number}) or, by default, a
NOISE-RELATIVE quality: the quality is then chosen automatically per channel, just before writing, so that the codec error is
DEFAULT_NOISE_FACTOR (1.0) x the pixel noise of that channel (see calibrate_noise_quality); with "ratio" so that the channel is about
`ratio` times smaller than the raw pixels (see calibrate_quality).
"""
import queue
import threading

import numpy as np

DEFAULT_NOISE_FACTOR = 1.0             # default of compression="lossy": codec error (rms, counts) = this x pixel noise of the channel
NOISE_ROW_STRIDE = 4                   # the noise is estimated on every 4th row (4x faster, 4x less memory, same value)
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


def _check_ratio(r):
    lo, hi = RATIO_RANGE
    try:
        r = float(r)
    except (TypeError, ValueError):
        raise StackIOError(f'ratio must be a number (or a dict {{channel: number}}), got {r!r}')
    if not (lo <= r <= hi):
        raise StackIOError(f'ratio must be between {lo:g} and {hi:g} (file size = raw size / ratio), got {r:g}')
    return r


def normalize_compression(compression, quality=None, ratio=None):
    """-> ('lossless', None, None) or ('lossy', quality_db_or_None, ratio_spec). Lossy without quality and ratio = noise-relative quality
    (codec error = DEFAULT_NOISE_FACTOR x noise, chosen per channel when writing). ratio_spec is None, a number (all channels) or a dict
    {channel name: number} (channels not in the dict get the noise-relative quality).
    Raises StackIOError for wrong values."""
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
        if ratio is None:
            return 'lossy', None, None
        if isinstance(ratio, dict):
            return 'lossy', None, {str(k): _check_ratio(v) for k, v in ratio.items()}
        return 'lossy', None, _check_ratio(ratio)
    raise StackIOError(f'compression must be "lossless" or "lossy", got {compression!r}')


def resolve_ratios(channels, ratio_spec):
    """Target compression per channel (list; None = noise-relative default quality) from a number / dict {channel: number}."""
    if isinstance(ratio_spec, dict):
        unknown = [k for k in ratio_spec if k not in channels]
        if unknown:
            raise StackIOError(f'ratio given for channel(s) {unknown} that are not in this call: {list(channels)}')
        return [float(ratio_spec[c]) if c in ratio_spec else None for c in channels]
    return [float(ratio_spec)] * len(channels)


def estimate_noise(img):
    """Pixel noise (standard deviation, counts) of one image, robust (MAD of differences of neighbouring pixels). Horizontal differences on
    every NOISE_ROW_STRIDE-th row and vertical differences on every NOISE_ROW_STRIDE-th column; the smaller of the two is used (image
    structure inflates a difference estimate, noise does not). Cost: ~0.1 s and ~50 MB temporary memory for a 2080x3088 frame."""
    a = np.asarray(img)
    s = NOISE_ROW_STRIDE

    def mad(d):
        d = d.ravel()
        return 0.0 if d.size == 0 else float(1.4826 * np.median(np.abs(d - np.median(d))) / np.sqrt(2))
    h = np.asarray(a[::s, :], dtype=np.float64)
    v = np.asarray(a[:, ::s], dtype=np.float64)
    return min(mad(h[:, 1:] - h[:, :-1]), mad(v[1:, :] - v[:-1, :]))


def _crop(img, n=1024):
    h, w = img.shape
    y0, x0 = max(0, (h - n) // 2), max(0, (w - n) // 2)
    return np.ascontiguousarray(img[y0:y0 + n, x0:x0 + n])


def calibrate_noise_quality(img, factor=None, verify=True, tol=0.1, max_iter=3):
    """Quality (dB) at which the JPEG2000 error of `img` (2D, uint16) is about `factor` x its pixel noise (default DEFAULT_NOISE_FACTOR).
    First guess from a formula: rmse = 65535 / 10^(dB/20). With verify=True (stacks and movies, one search per file) the guess is checked by
    encoding + decoding and corrected (usually 1-2 rounds, ~1 s each on a full frame); with verify=False (single images) only the formula is used
    (no extra encoding). The quality is limited to QUALITY_RANGE_DB. Returns (quality_db, dict(quality_db, ratio, rmse, noise))."""
    factor = DEFAULT_NOISE_FACTOR if factor is None else float(factor)
    lo, hi = QUALITY_RANGE_DB
    noise = estimate_noise(img)
    target = max(factor * noise, 1e-3)
    q = float(np.clip(20 * np.log10(65535.0 / target), lo, hi))
    info = dict(quality_db=round(q, 2), ratio=None, rmse=None, noise=noise)
    if not verify:
        return q, info
    import imagecodecs
    img = np.ascontiguousarray(img)
    ref = img.astype(np.float64)
    for _ in range(max_iter):
        b = imagecodecs.jpeg2k_encode(img, level=q, reversible=False, numthreads=4)
        rmse = float(np.sqrt(np.mean((imagecodecs.jpeg2k_decode(b).astype(np.float64) - ref) ** 2)))
        info.update(quality_db=round(q, 2), ratio=round(img.nbytes / len(b), 1), rmse=rmse)
        if rmse <= 0 or abs(rmse / target - 1) <= tol or q in (lo, hi):
            break
        q2 = float(np.clip(q + 20 * np.log10(rmse / target), lo, hi))     # error too big -> raise the quality
        if q2 == q:
            break
        q = q2
    return q, info


def calibrate_quality(data, ratios, tol_db=1.0):
    """Quality (dB) per channel that gives about `ratio` x compression. data: (T, Z, C, Y, X). For every channel the middle plane is tested
    (whole plane, bisection on the quality to 1 dB; a crop was not representative: it gave 55x instead of 100x). Every channel gets its OWN
    quality (a common one set by the least compressible channel destroyed the easy channels). Returns (list of quality_db per channel, info)
    with info = per channel dict(quality_db, ratio, rmse, noise). A ratio of None means: noise-relative default (calibrate_noise_quality)."""
    import imagecodecs
    T, Z, C = data.shape[:3]
    lo_q, hi_q = QUALITY_RANGE_DB
    infos, chosen = [], []
    ratios = [float(ratios)] * C if np.isscalar(ratios) else list(ratios)
    for c in range(C):
        ratio = ratios[c]
        img = np.ascontiguousarray(data[T // 2, Z // 2, c])
        if ratio is None:                                  # no target for this channel: noise-relative default
            q, info = calibrate_noise_quality(img, verify=(T * Z > 1))
            info['channel_index'] = c
            chosen.append(q)
            infos.append(info)
            continue
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
    return [float(q) for q in chosen], infos


class PageFiles:
    """Pages (T, Z, C) that are stored as single-page TIFF files, behaving like a read-only array (T, Z, C, Y, X) for write_ome_tiff:
    a page is read from disk when it is indexed, so only one page is in memory at a time. A missing file reads as zeros (see .missing)."""

    def __init__(self, paths, shape_tzc, dtype, yx):
        self.paths = {tuple(k): v for k, v in paths.items()}
        self.shape = tuple(shape_tzc) + tuple(yx)
        self.dtype = np.dtype(dtype)
        self.ndim = 5
        self.nbytes = int(np.prod(self.shape)) * self.dtype.itemsize
        self.missing = []

    def __getitem__(self, key):
        import os
        import tifffile
        p = self.paths.get(tuple(key))
        if p is None or not os.path.isfile(p):
            self.missing.append(tuple(key))
            return np.zeros(self.shape[3:], self.dtype)
        a = tifffile.imread(p)
        return np.ascontiguousarray(a.reshape(self.shape[3:]))


def merge_ok(src_paths, out_path, n_pages):
    """True if out_path is a readable TIFF with n_pages pages (checked before the single files are deleted)."""
    import os
    import tifffile
    try:
        if not os.path.isfile(out_path):
            return False
        with tifffile.TiffFile(out_path) as tf:
            return len(tf.pages) == n_pages
    except Exception:
        return False


def check_lossy_ok():
    ok, msg = lossy_available()
    if not ok:
        raise StackIOError(msg)


def _tiff_args(mode, quality, height):
    if mode == 'lossy':
        # ONE strip per page: tifffile's default (strips of ~64 kB, each coded on its own) made the same quality 8x larger
        return dict(compression='jpeg2000', compressionargs={'level': float(quality), 'numthreads': 4}, rowsperstrip=int(height))
    return dict(compression='zlib', compressionargs={'level': ZLIB_LEVEL})


def write_ome_tiff(path, data, channels, planes=None, pixel_size_um=None, z_step_um=None, time_increment_s=None,
                   compression='lossless', quality=None, ratio=None, description=None):
    """Write `data` (T, Z, C, Y, X) as a multipage OME-TIFF.

    compression: "lossless" (zlib), "lossy" (JPEG2000) or "none" (uncompressed). channels: list of C names. planes: optional dict of lists with one entry per page in file order
    (keys: time_s, z_um, x_um, y_um, exposure_ms). description: free text stored as the image description (OME)."""
    import tifffile
    raw = isinstance(compression, str) and compression.strip().lower() in ('none', 'raw', 'uncompressed')   # no compression at all (fast movies)
    mode, q, ratio = normalize_compression('lossless' if raw else compression, quality, ratio)
    if mode == 'lossy':
        check_lossy_ok()
    data = data if isinstance(data, PageFiles) else np.asarray(data)
    if data.ndim != 5:
        raise StackIOError(f'data must have axes T,Z,C,Y,X, got shape {data.shape}')
    T, Z, C = data.shape[:3]
    if len(channels) != C:
        raise StackIOError(f'{len(channels)} channel names for {C} channels')
    n = T * Z * C
    targets = resolve_ratios(channels, ratio) if (mode == 'lossy' and ratio is not None) else None
    info = dict(mode=mode, quality_db=q, target_ratio=ratio, targets=targets, raw_bytes=int(data.nbytes))
    qs = [q] * C
    if mode == 'lossy' and q is None:                       # ratio targets and/or noise-relative default, chosen per channel
        qs, per_channel = calibrate_quality(data, targets if targets is not None else [None] * C)
        info['channels'] = per_channel
        info['noise_factor'] = DEFAULT_NOISE_FACTOR
    if mode == 'lossy':
        info['qualities'] = qs
        info['quality_db'] = min(qs)
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
    if mode == 'lossless' and isinstance(data, np.ndarray):
        tifffile.imwrite(path, data, ome=True, metadata=meta, bigtiff=True, **({} if raw else _tiff_args(mode, None, data.shape[-2])))
    else:
        # lossy: every page is written separately, because every channel has its own quality (tifffile applies one compression to a call);
        # also used for lossless data that is read page by page from files (PageFiles)
        if description and mode == 'lossy':
            meta['Description'] = f'{description}; JPEG2000 quality ' + ', '.join(f'{c}: {v:.0f} dB' for c, v in zip(channels, qs)) + \
                (' (chosen per channel for target compressions ' + ', '.join(f'{t:g}x' if t else f'error = {DEFAULT_NOISE_FACTOR:g} x noise' for t in targets) + ')' if targets
                 else (f' (chosen per channel: error = {DEFAULT_NOISE_FACTOR:g} x pixel noise)' if q is None else ''))
        ox = tifffile.OmeXml()
        ox.addimage(dtype=data.dtype, shape=data.shape, storedshape=(n, 1, 1, data.shape[-2], data.shape[-1], 1), **meta)
        xml = ox.tostring().encode('ascii', 'xmlcharrefreplace').decode('ascii')     # TIFF descriptions must be 7-bit ASCII
        with tifffile.TiffWriter(path, bigtiff=True, ome=False) as tw:
            k = 0
            for t in range(T):
                for z in range(Z):
                    for c in range(C):
                        tw.write(data[t, z, c], photometric='minisblack', metadata=None, contiguous=False,
                                 description=xml if k == 0 else None, **({} if raw else _tiff_args(mode, qs[c], data.shape[-2])))
                        k += 1
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
