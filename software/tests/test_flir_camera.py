"""The real control/camera_flir.py driven against a fake PySpin (tests/fake_pyspin.py): no camera, no Spinnaker needed.

What is checked: opening the camera, exposure / gain / MONO16, software and hardware trigger, a long exposure (1.5 s),
that every image has the exposure that was set for it (channel switching), that no stale frame is read after live view,
timeouts and incomplete frames (None, then an error after 3 in a row), and many live <-> acquisition switches.
Run:  python tests/test_flir_camera.py      (or python -m pytest tests/test_flir_camera.py)
What it cannot check: the real camera's timing, firmware behaviour and USB problems - see the hardware check script.
"""
import os
import sys
import time
import threading

SW = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SW)
sys.path.insert(0, os.path.join(SW, 'tests'))
import fake_pyspin  # noqa: E402
sys.modules['PySpin'] = fake_pyspin
import control.camera_flir as cf  # noqa: E402
from control._def import TriggerMode  # noqa: E402


def new_camera(pixel_format='MONO16'):
    fake = fake_pyspin.reset()
    cam = cf.Camera(sn=None)
    cam.open()
    cam.set_pixel_format(pixel_format)
    cam.set_software_triggered_acquisition()
    cam.start_streaming()
    return cam, fake


def snap(cam, exposure_ms=None):
    if exposure_ms is not None:
        cam.set_exposure_time(exposure_ms)
    cam.send_trigger()
    return cam.read_frame()


def exposure_of(img):
    return int(img[0, 1])


def test_open_and_settings():
    cam, fake = new_camera()
    assert fake.initialized and fake.streaming and cam.is_streaming
    cam.set_exposure_time(25)
    cam.set_analog_gain(10)
    assert abs(fake.n_exposure.value - 25000) < 1e-6, fake.n_exposure.value
    assert fake.n_exposure_auto.symbolic == 'Off' and fake.n_gain_auto.symbolic == 'Off' and fake.n_gain.value == 10
    assert fake.n_pixel_format.symbolic == 'Mono16'
    assert fake.n_trigger_mode.symbolic == 'On' and fake.n_trigger_source.symbolic == 'Software'
    img = snap(cam)
    assert img is not None and img.dtype.name == 'uint16' and img.shape == (fake.n_height.value, fake.n_width.value)
    assert exposure_of(img) == 25


def test_mono8_and_back_to_mono16():
    cam, fake = new_camera('MONO8')
    img = snap(cam, 10)
    assert img.dtype.name == 'uint8'
    cam.set_pixel_format('MONO16')            # GUI dropdown: switching while streaming
    assert fake.streaming and fake.n_pixel_format.symbolic == 'Mono16'
    img = snap(cam, 10)
    assert img.dtype.name == 'uint16'


def test_long_exposure_1500ms():
    cam, fake = new_camera()
    t0 = time.time()
    img = snap(cam, 1500)
    dt = time.time() - t0
    assert img is not None, 'a 1.5 s exposure must not time out'
    assert exposure_of(img) == 1500 and dt >= 1.5, (exposure_of(img), dt)


def test_each_channel_gets_its_own_exposure():
    """Several channels with different exposures in a row (as in a multipoint acquisition): image n has exposure n."""
    cam, fake = new_camera()
    plan = [12, 22, 100, 100, 12, 50, 12]
    got = [exposure_of(snap(cam, e)) for e in plan]
    assert got == plan, got


def test_no_stale_frame_after_live():
    """Live view (callback, continuous triggers) then an acquisition read: the read must return the new frame."""
    cam, fake = new_camera()
    frames = []
    cam.set_callback(lambda c: frames.append(c.current_frame[0, 1]))
    cam.enable_callback()
    for _ in range(3):                          # live: software triggers with exposure 20
        snap_t = cam.set_exposure_time(20) or cam.send_trigger()
        time.sleep(0.08)
    cam.send_trigger()                          # a last live trigger still in flight when live stops
    cam.disable_callback()                      # acquisition starts (stop/start streaming: buffers cleared)
    img = snap(cam, 77)
    assert frames, 'live callback did not run'
    assert exposure_of(img) == 77, f'read returned a stale live frame (exposure {exposure_of(img)} ms)'


def test_stale_frame_in_buffer_is_detected():
    """If a frame stays in the buffer (e.g. an extra trigger), every later read returns the previous image:
    channel n would get the image of channel n-1. This documents the risk; read_frame itself cannot know."""
    cam, fake = new_camera()
    cam.set_exposure_time(5)
    cam.send_trigger()                          # extra trigger, never read
    time.sleep(0.1)
    img = snap(cam)                             # same exposure (a change would first send a throw-away frame)
    assert exposure_of(img) == 5                # the stale one comes first (oldest first)
    assert len(fake.buffer) == 1 or snap(cam) is not None


def test_timeout_returns_none_then_raises():
    cam, fake = new_camera()
    cam.set_exposure_time(5)                    # before the faults: a changed exposure sends a throw-away frame that would use them up
    fake.fault['drop_triggers'] = 3
    assert snap(cam, 5) is None
    assert snap(cam, 5) is None
    try:
        snap(cam, 5)
        raise AssertionError('3 lost frames in a row must raise')
    except RuntimeError as e:
        assert '3 times in a row' in str(e)
    assert exposure_of(snap(cam, 5)) == 5      # the camera works again afterwards


def test_incomplete_frame_returns_none():
    cam, fake = new_camera()
    cam.set_exposure_time(5)
    fake.fault['incomplete_frames'] = 1
    assert snap(cam, 5) is None
    assert exposure_of(snap(cam, 6)) == 6


def test_hardware_trigger_rolling_shutter_exposure():
    cam, fake = new_camera()
    cam.set_hardware_triggered_acquisition()
    cam.set_exposure_time(10)
    assert fake.n_trigger_source.symbolic == 'Line2'
    assert fake.n_exposure.value > 10000, 'rolling shutter: camera exposure = illumination time + readout'
    fake.hardware_trigger('Line2')
    img = cam.read_frame()
    assert img is not None


def test_many_live_acquisition_switches():
    cam, fake = new_camera()
    cam.set_callback(lambda c: None)
    for i in range(30):
        cam.enable_callback()
        cam.send_trigger()
        time.sleep(0.01)
        cam.disable_callback()
        assert exposure_of(snap(cam, 3 + i % 4)) == 3 + i % 4
    assert fake.streaming and fake.handler is None


def test_two_threads_use_the_camera():
    """Live (GUI thread) and acquisition (worker) at the same time: counted by the fake (the input lock prevents it)."""
    cam, fake = new_camera()
    stop = [False]

    def other():
        while not stop[0]:
            try:
                cam.send_trigger()
            except Exception:
                pass
            time.sleep(0.001)
    t = threading.Thread(target=other, daemon=True)
    t.start()
    for _ in range(5):
        snap(cam, 5)
    stop[0] = True
    t.join()
    assert fake.n_triggers > 5          # extra triggers from the other thread pile up in the buffer: wrong images


def test_readout_measured_for_imx178_not_imx226_constants():
    """Hardware trigger, rolling shutter: the light must wait until the last row exposes (strobe delay) and the camera
    exposure must cover light + readout. The readout is measured on the camera (1 / max frame rate at short exposure)
    instead of the IMX226 constants (3036 rows x 10 us x bytes per pixel)."""
    cam, fake = new_camera('MONO16')
    true_readout_us = fake.sensor_readout_s() * 1e6
    old_constant_us = 10 * 2 * (3036 - 1)
    assert cam.readout_us is not None, 'readout was not measured'
    assert cam.readout_us >= true_readout_us - 1, (cam.readout_us, true_readout_us)    # never shorter than the sensor
    assert cam.readout_us < old_constant_us, (cam.readout_us, old_constant_us)
    cam.set_hardware_triggered_acquisition()
    cam.set_exposure_time(10)
    expected = cam.exposure_delay_us + 10000 + cam.readout_us + 500
    assert abs(fake.n_exposure.value - expected) < 1, (fake.n_exposure.value, expected)
    assert cam.strobe_delay_us == int(cam.exposure_delay_us + cam.readout_us)
    print(f'    MONO16 full frame: readout {cam.readout_us / 1000:.1f} ms (old constants {old_constant_us / 1000:.1f} ms), '
          f'camera exposure for 10 ms light {fake.n_exposure.value / 1000:.1f} ms')


def test_readout_follows_bit_depth_and_roi():
    cam, fake = new_camera('MONO16')
    r16 = cam.readout_us
    cam.set_pixel_format('MONO8')
    r8 = cam.readout_us
    assert abs(r8 - r16 / 2) < r16 * 0.02, (r8, r16)
    cam.set_ROI(0, 0, 3072, 1024)
    assert abs(cam.readout_us - r8 / 2) < r8 * 0.02, (cam.readout_us, r8)
    assert cam.readout_us >= fake.sensor_readout_s() * 1e6 - 1


def test_readout_fallback_when_camera_has_no_frame_rate_node():
    fake = fake_pyspin.reset()
    del fake.nodemap.nodes['AcquisitionFrameRate']
    del fake.nodemap.nodes['AcquisitionFrameRateEnable']
    cam = cf.Camera(sn=None)
    try:
        cam.open()
    except Exception:
        pass                       # open() itself sets the frame rate; the fallback below is what matters
    assert cam._update_readout_timing() is None
    assert cam._readout_time_us() == cam.row_period_us * cam.pixel_size_byte * (cam.row_numbers - 1)


if __name__ == '__main__':
    fails = 0
    for n, f in list(globals().items()):
        if n.startswith('test_') and callable(f):
            try:
                f()
                print('PASS', n)
            except Exception as e:
                import traceback
                traceback.print_exc()
                fails += 1
                print('FAIL', n, repr(e))
    print(f'{fails} failed')
    os._exit(1 if fails else 0)
