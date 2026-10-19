"""Flexible Multipoint: single files merged into the multichannel file (control.towbin_funs.merge_plane_tiffs) give the same file as
save_single_plane_tiff(array (C, Z, Y, X)), the single files are deleted only after a successful merge. No Qt, no hardware."""
import os
import re
import sys
import tempfile

import numpy as np
import tifffile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from control.towbin_funs import save_single_plane_tiff, merge_plane_tiffs        # noqa: E402


def _strip_date(xml):
    xml = re.sub(r'AcquisitionDate>[^<]*<', 'AcquisitionDate><', xml)
    return re.sub(r'(ID="\w+:)\d+"', r'\1"', xml)             # the auto-numbered IDs differ


def _stack(C=3, Z=2, H=40, W=50):
    rng = np.random.default_rng(1)
    return rng.integers(0, 4000, (C, Z, H, W)).astype(np.uint16)


def test_merge_equals_direct_multichannel_file():
    arr = _stack()
    d = tempfile.mkdtemp()
    names = ['BF', 'Fluorescence 488 nm Ex', 'Fluorescence 561 nm Ex']
    parts = {}
    for c in range(3):
        for z in range(2):
            p = os.path.join(d, f'A1_0_0_{z}_{c}.tiff')
            save_single_plane_tiff(arr[c, z], p)
            parts[(c, z)] = p
    direct = os.path.join(d, 'direct.tiff')
    save_single_plane_tiff(arr, direct, channels=names)
    merged = os.path.join(d, 'merged.tiff')
    ok, missing = merge_plane_tiffs(parts, merged, names, 3, 2)
    assert ok and not missing
    assert np.array_equal(tifffile.imread(merged), tifffile.imread(direct))
    with tifffile.TiffFile(direct) as a, tifffile.TiffFile(merged) as b:
        assert len(a.pages) == len(b.pages) == 6 and a.pages[0].compression == b.pages[0].compression
        assert _strip_date(a.ome_metadata) == _strip_date(b.ome_metadata)
    assert not any(os.path.exists(p) for p in parts.values()), 'single files are deleted after the merge'
    assert not os.path.exists(merged + '.part')


def test_missing_single_file_keeps_the_others_and_writes_zeros():
    arr = _stack(C=2, Z=1)
    d = tempfile.mkdtemp()
    p0 = os.path.join(d, 'a.tiff')
    save_single_plane_tiff(arr[0, 0], p0)
    ok, missing = merge_plane_tiffs({(0, 0): p0, (1, 0): os.path.join(d, 'nope.tiff')}, os.path.join(d, 'm.tiff'), ['A', 'B'], 2, 1)
    assert not ok and missing == [(1, 0)] and os.path.exists(p0)
    back = tifffile.imread(os.path.join(d, 'm.tiff')).reshape(2, 40, 50)
    assert np.array_equal(back[0], arr[0, 0]) and back[1].max() == 0


def test_failure_leaves_single_files_and_no_partial_file():
    d = tempfile.mkdtemp()
    arr = _stack(C=1, Z=1)
    p0 = os.path.join(d, 'a.tiff')
    save_single_plane_tiff(arr[0, 0], p0)
    bad = os.path.join(d, 'no_such_dir', 'm.tiff')
    try:
        merge_plane_tiffs({(0, 0): p0}, bad, ['A'], 1, 1)
        raise AssertionError('should fail')
    except (OSError, FileNotFoundError):
        pass
    assert os.path.exists(p0) and not os.path.exists(bad + '.part')


if __name__ == '__main__':
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print('PASS', name)
            except Exception as e:
                import traceback
                traceback.print_exc()
                fails += 1
                print('FAIL', name, repr(e))
    sys.exit(1 if fails else 0)
