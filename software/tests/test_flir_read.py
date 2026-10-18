"""FLIR read_frame stability (no camera, PySpin is faked): timeout follows the exposure, a timeout or an incomplete
frame returns None instead of a black image, three failures in a row raise.   Run: python tests/test_flir_read.py"""
import os
import sys
import types

import numpy as np

SW = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SW)


class SpinnakerException(Exception):
    pass


fake = types.ModuleType('PySpin')
fake.SpinnakerException = SpinnakerException
fake.ImageEventHandler = object
fake.__getattr__ = lambda name: (lambda *a, **k: None)   # any other PySpin name used at import time
sys.modules.setdefault('PySpin', fake)
import control.camera_flir as cf  # noqa: E402


class Img:
    def __init__(self, incomplete=False):
        self.incomplete, self.released = incomplete, False
    def IsIncomplete(self): return self.incomplete
    def GetImageStatus(self): return 7
    def Release(self): self.released = True
    def GetNDArray(self): return np.ones((4, 6), dtype=np.uint16)


class Cam:
    def __init__(self, script): self.script, self.timeouts = list(script), []
    def IsStreaming(self): return True
    def GetNextImage(self, t):
        self.timeouts.append(t)
        x = self.script.pop(0)
        if x == 'timeout':
            raise SpinnakerException('Spinnaker: Failed waiting for EventData on NEW_BUFFER_DATA event. [-1011]')
        return Img(incomplete=(x == 'incomplete'))


def make(script, exposure_ms):
    c = object.__new__(cf.Camera)
    c.camera_list = types.SimpleNamespace(Clear=lambda: None)
    c.system = c.py_spin_system = types.SimpleNamespace(ReleaseInstance=lambda: None)
    c.camera = Cam(script)
    c.callback_is_enabled = False
    c.is_color = False
    c.convert_pixel_format = False
    c.pixel_format = 'MONO16'
    c.exposure_time = exposure_ms
    c.Width, c.Height = 6, 4
    return c


def test_timeout_follows_exposure():
    c = make(['ok'], 1500)
    img = c.read_frame()
    assert img is not None and img.shape == (4, 6)
    assert c.camera.timeouts[0] >= 1500 + 900, c.camera.timeouts


def test_timeout_and_incomplete_return_none_then_raise():
    c = make(['timeout', 'incomplete', 'ok', 'timeout', 'timeout', 'timeout'], 10)
    assert c.read_frame() is None
    assert c.read_frame() is None
    assert c.read_frame() is not None            # a good frame resets the failure count
    assert c.read_frame() is None and c.read_frame() is None
    try:
        c.read_frame()
        raise AssertionError('third failure in a row should raise')
    except RuntimeError as e:
        assert '3 times in a row' in str(e)


if __name__ == '__main__':
    fails = 0
    for n, f in sorted(globals().items()):
        if n.startswith('test_'):
            try:
                f(); print('PASS', n)
            except Exception as e:
                import traceback; traceback.print_exc(); fails += 1; print('FAIL', n, e)
    sys.exit(1 if fails else 0)
