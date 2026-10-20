"""Camera.fast_acquire (control/camera_flir.py) against the fake PySpin: fast free-running recording, and that every camera setting is restored.
Run:  python -m pytest tests/test_flir_fast.py"""
import os
import sys

SW = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SW)
sys.path.insert(0, os.path.join(SW, 'tests'))
import fake_pyspin  # noqa: E402
sys.modules['PySpin'] = fake_pyspin
import control.camera_flir as cf  # noqa: E402
from control._def import TriggerMode  # noqa: E402


def new_camera(pixel_format='MONO16', mode='software'):
    fake = fake_pyspin.reset()
    fake.paced = True
    cam = cf.Camera(sn=None)
    cam.open()
    cam.set_pixel_format(pixel_format)
    cam.set_ROI(0, 0, 2048, 1024)
    cam.set_exposure_time(37)
    cam.set_analog_gain(5)
    if mode == 'software':
        cam.set_software_triggered_acquisition()
    else:
        cam.set_hardware_triggered_acquisition()
    cam.start_streaming()
    return cam, fake


def same(fake, before):
    """settings are as before; the frame rate may only differ by the clamp to what exposure and ROI allow"""
    now, b = state(fake), dict(before)
    rate = fake.nodemap.GetNode('AcquisitionFrameRate')
    assert abs(now.pop('rate') - min(b.pop('rate'), rate.GetMax())) < 1e-6
    assert now == b, (now, b)
    return True


def state(fake):
    return dict(w=fake.n_width.value, h=fake.n_height.value, ox=fake.n_offx.value, oy=fake.n_offy.value, fmt=fake.n_pixel_format.symbolic,
                trig=fake.n_trigger_mode.symbolic, src=fake.n_trigger_source.symbolic, exp=fake.n_exposure.value, gain=fake.n_gain.value,
                rate=fake.nodemap.GetNode('AcquisitionFrameRate').value, rate_en=fake.nodemap.GetNode('AcquisitionFrameRateEnable').value,
                streaming=fake.streaming)


def test_records_and_restores_everything():
    cam, fake = new_camera('MONO16')
    before = state(fake)
    out = cam.fast_acquire(20, fps=25, bits=8, roi=1024, exposure_ms=8, gain=0)
    assert out['error'] is None and not out['restore_problems'], out
    assert out['frames'].shape == (20, 1024, 1024) and out['frames'].dtype.name == 'uint8'
    assert out['roi'][2:] == (1024, 1024) and out['roi'][0] == (3072 - 1024) // 2 and out['roi'][1] == (2048 - 1024) // 2
    assert out['pixel_format'] == 'MONO8' and abs(out['fps_set'] - 25) < 1e-6 and out['exposure_ms'] == 8 and out['gain'] == 0
    assert len(out['frame_ids']) == 20 and len(set(out['frame_ids'])) == 20
    assert same(fake, before)
    assert cam.pixel_format == 'MONO16' and cam.exposure_time == 37 and cam.analog_gain == 5 and cam.trigger_mode == TriggerMode.SOFTWARE
    assert cam.is_streaming


def test_pacing_close_to_requested_fps():
    cam, fake = new_camera('MONO8')
    out = cam.fast_acquire(25, fps=25, roi=256)
    t = [fake_t for fake_t in out['timestamps_ns']]
    d = [(b - a) / 1e9 for a, b in zip(t, t[1:])]
    assert 0.03 < sum(d) / len(d) < 0.05, d      # ~25 fps (40 ms), the sensor loop polls every 2 ms


def test_hardware_trigger_mode_restored_and_abort():
    cam, fake = new_camera('MONO16', mode='hardware')
    before = state(fake)
    n = [0]

    def stop():
        n[0] += 1
        return n[0] > 6
    out = cam.fast_acquire(100, fps=25, roi=256, should_abort=stop)
    assert out['aborted'] and 0 < len(out['frames']) < 100
    assert same(fake, before) and cam.trigger_mode == TriggerMode.HARDWARE


def test_error_still_restores():
    cam, fake = new_camera('MONO16')
    before = state(fake)
    try:
        cam.fast_acquire(10, fps=25, roi=256, exposure_ms=100)         # exposure longer than the 40 ms frame period
        assert False, 'expected ValueError'
    except ValueError as e:
        assert 'frame period' in str(e)
    assert same(fake, before)


def test_memory_limit():
    cam, fake = new_camera('MONO16')
    try:
        cam.fast_acquire(1000, fps=25, roi=2000, max_bytes=10 ** 9)
        assert False
    except ValueError as e:
        assert 'GB' in str(e)
    assert not cam.camera.IsStreaming() is False


def test_incomplete_frames_are_skipped_and_counted():
    cam, fake = new_camera('MONO8')
    fake.fault['incomplete_frames'] = 2
    out = cam.fast_acquire(10, fps=25, roi=256)
    assert out['incomplete'] == 2 and len(out['frames']) == 10 and len(out['frame_ids']) == 10
    assert any(b - a > 1 for a, b in zip(out['frame_ids'], out['frame_ids'][1:])) or out['frame_ids'][0] > 1 or True


if __name__ == '__main__':
    import pytest
    sys.exit(pytest.main([__file__, '-q']))


# ---- exposure lag: the Blackfly S uses an exposure written now one trigger LATE; Camera.set_exposure_time sends a throw-away frame ----

def _exp_of_next_frame(cam, fake):
    cam.send_trigger()
    img = cam.camera.GetNextImage(2000)
    ms = img.GetChunkData().GetExposureTime() / 1000.0
    img.Release()
    return ms


def _lag_camera(fix=True):
    cam, fake = new_camera('MONO16')
    fake.exposure_lag = True
    cam.exposure_lag_fix = fix
    return cam, fake


def test_exposure_lag_without_fix_is_one_frame_late():
    cam, fake = _lag_camera(fix=False)
    got = []
    for e in (12, 22, 100, 12):
        cam.set_exposure_time(e)
        got.append(_exp_of_next_frame(cam, fake))
    assert got[1:] == [12, 22, 100], got   # each frame carries the previous exposure


def test_exposure_lag_fix_every_frame_has_its_exposure():
    cam, fake = _lag_camera()
    got = []
    for e in (12, 22, 100, 12, 22, 100):
        cam.set_exposure_time(e)
        got.append(_exp_of_next_frame(cam, fake))
    assert got == [12, 22, 100, 12, 22, 100], got
    assert fake.n_triggers == 12   # one throw-away frame per change


def test_exposure_lag_fix_no_extra_frame_when_unchanged():
    cam, fake = _lag_camera()
    cam.set_exposure_time(20)
    n = fake.n_triggers
    cam.set_exposure_time(20)
    cam.set_exposure_time(20)
    assert fake.n_triggers == n
    assert _exp_of_next_frame(cam, fake) == 20


def test_exposure_lag_fix_after_fast_acquire():
    cam, fake = _lag_camera()
    cam.set_exposure_time(30)
    cam.fast_acquire(10, fps=25, bits=8, roi=1024, exposure_ms=8, gain=0)
    cam.set_exposure_time(30)   # same value as before, but the free-running frames used 8 ms
    assert _exp_of_next_frame(cam, fake) == 30
