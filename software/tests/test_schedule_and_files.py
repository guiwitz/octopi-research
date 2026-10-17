"""Checks that the independent expectations used by e2e_acquisition.py agree with the code.
Run: python tests/test_schedule_and_files.py   (or python -m pytest tests)"""
import os, sys, json, tempfile, types
HERE = os.path.dirname(os.path.abspath(__file__)); SW = os.path.dirname(HERE)
os.chdir(SW); sys.path.insert(0, SW); sys.path.insert(0, HERE)
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from unittest.mock import MagicMock
for m in ["napari", "napari.utils", "napari.utils.colormaps"]:
    sys.modules.setdefault(m, MagicMock())
from qtpy.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])
import control.core as core
import e2e_acquisition as e2e


def test_expected_af_matches_controller():
    c = core.AutoFocusController(None, MagicMock(), MagicMock())
    for mode in ('every_timepoint', 'every_n', 'staggered'):
        for n in (1, 2, 3, 10):
            for t0 in (True, False):
                c.set_af_schedule(mode=mode, n=n, all_at_t0=t0)
                got = {(t, p) for t in range(12) for p in range(25) if c.autofocus_scheduled(t, p)}
                assert got == e2e.expected_af(mode, n, t0, 12, 25), (mode, n, t0)


def test_expected_af_spec_examples():
    s = e2e.expected_af('staggered', 10, False, 3, 25)
    assert {p for t, p in s if t == 1} == {1, 11, 21} and {p for t, p in s if t == 2} == {2, 12, 22}
    assert {p for t, p in e2e.expected_af('staggered', 10, True, 3, 25) if t == 0} == set(range(25))
    assert e2e.expected_af('every_n', 3, True, 7, 2) == {(t, p) for t in (0, 3, 6) for p in (0, 1)}
    assert e2e.expected_af('every_timepoint', 1, True, 2, 2, af_on=False) == set()


def test_check_files(tmp_path=None):
    d = tempfile.mkdtemp()
    json.dump({'Nt': 2}, open(os.path.join(d, 'acquisition parameters.json'), 'w'))
    open(os.path.join(d, 'configurations.xml'), 'w').write('x')
    for t in range(2):
        os.makedirs(os.path.join(d, str(t)))
        for p in range(3):
            for ch in ('BF', 'GFP'):
                open(os.path.join(d, str(t), f'{p}_0_0_0_{ch}.tiff'), 'w').write('x')
    assert all(ok for _, ok, _ in e2e.check_files(d, 2, 3, 2, False))
    os.remove(os.path.join(d, '1', '2_0_0_0_GFP.tiff'))
    assert not all(ok for _, ok, _ in e2e.check_files(d, 2, 3, 2, False))


if __name__ == '__main__':
    test_expected_af_matches_controller(); test_expected_af_spec_examples(); test_check_files()
    print('ALL PASSED')
