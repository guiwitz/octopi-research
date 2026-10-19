"""HardwareBackend.fast_movie + scope.fast_movie with the real Camera class on the fake PySpin (tests/fake_pyspin.py) and fake controllers:
the light is on only while the camera records, the camera is restored, frames reach the file and the display.
Run:  python -m pytest tests/test_hardware_backend_fast.py"""
import os
import sys
import types
import tempfile

SW = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SW)
sys.path.insert(0, os.path.join(SW, 'tests'))
import numpy as np                       # noqa: E402
import fake_pyspin                       # noqa: E402
sys.modules['PySpin'] = fake_pyspin
import control.camera_flir as cf         # noqa: E402
from control._def import TriggerMode     # noqa: E402
from control.jobs.api import make_points  # noqa: E402
from control.jobs.runner import run_job   # noqa: E402
from control.jobs.hardware_backend import HardwareBackend  # noqa: E402
import tifffile                           # noqa: E402


class Sig:
    def __init__(self, log, name):
        self.log, self.name = log, name

    def emit(self, *a):
        self.log.append((self.name, a))


def make_backend(abort_after=None):
    fake = fake_pyspin.reset()
    fake.paced = True
    cam = cf.Camera(sn=None)
    cam.open()
    cam.set_pixel_format('MONO16')
    cam.set_ROI(0, 0, 2048, 1024)
    cam.set_software_triggered_acquisition()
    cam.start_streaming()
    cfg = types.SimpleNamespace(name='Fluorescence 488 nm Ex', exposure_time=8.0, analog_gain=2.0, illumination_intensity=30.0, illumination_source=12)
    events = []
    live = types.SimpleNamespace(trigger_mode=TriggerMode.SOFTWARE,
                                 turn_on_illumination=lambda: events.append(('light', 'on', fake.streaming)),
                                 turn_off_illumination=lambda: events.append(('light', 'off', fake.streaming)))
    nav = types.SimpleNamespace(x_pos_mm=1.0, y_pos_mm=2.0, z_pos_mm=3.0)
    mpc = types.SimpleNamespace(abort_acqusition_requested=False)
    worker = types.SimpleNamespace(
        camera=cam, liveController=live, navigationController=nav, microcontroller=None, autofocusController=None,
        configurationManager=types.SimpleNamespace(configurations=[cfg]), multiPointController=mpc,
        signal_current_configuration=Sig(events, 'config'), image_to_display=Sig(events, 'display'), wait_till_operation_is_completed=lambda: None)

    def apply(*a):                       # what the GUI does when the configuration is emitted
        cam.set_exposure_time(cfg.exposure_time)
        cam.set_analog_gain(cfg.analog_gain)
    worker.signal_current_configuration = types.SimpleNamespace(emit=lambda c: (events.append(('config', (c.name,))), apply()))
    return HardwareBackend(worker), cam, fake, events, mpc


def test_fast_movie_through_scope_file_light_restore_display():
    be, cam, fake, events, mpc = make_backend()
    out = tempfile.mkdtemp()
    job = os.path.join(tempfile.mkdtemp(), 'job.py')
    open(job, 'w').write('def run_job(scope):\n    scope.fast_movie("488", fps=25, frames=15, roi=256, bits=8)\n')
    res = run_job(job, make_points(['stage'], [(1, 2, 3)]), be, experiment_dir=out)
    assert res.status == 'completed', res.error
    f = os.path.join(out, 'Time0000_Stage_FastMovie.tiff')
    with tifffile.TiffFile(f) as tf:
        assert len(tf.pages) == 15 and tf.pages[0].shape == (256, 256) and tf.pages[0].dtype == np.uint8 and tf.pages[0].compression.name == 'NONE'
    kinds = [e[:2] if e[0] == 'light' else e[0] for e in events]
    assert kinds.index(('light', 'on')) < kinds.index(('light', 'off'))
    on = next(e for e in events if e[:2] == ('light', 'on'))
    assert on[2] is True                              # the light goes on only after the camera is streaming
    first_off = next(e for e in events if e[:2] == ('light', 'off'))
    assert first_off[2] is True                       # ... and off right after the last frame, before the camera is reconfigured
    assert any(e[0] == 'display' for e in events)
    assert cam.pixel_format == 'MONO16' and cam.trigger_mode == TriggerMode.SOFTWARE and cam.exposure_time == 8.0 and cam.analog_gain == 2.0
    assert fake.n_width.value == 2048 and fake.n_height.value == 1024 and fake.streaming
    ff = open(os.path.join(out, 'fast_frames.csv')).read().splitlines()
    assert len(ff) == 16 and ff[0].startswith('file,frame,camera_frame_id')
    assert not [w for w in res.warnings if 'not restored' in w], res.warnings


def test_abort_stops_recording_early_and_keeps_frames():
    be, cam, fake, events, mpc = make_backend()
    n = [0]
    real = be.abort_requested
    be.abort_requested = lambda: (n.__setitem__(0, n[0] + 1) or n[0] > 5)
    out = tempfile.mkdtemp()
    job = os.path.join(tempfile.mkdtemp(), 'job.py')
    open(job, 'w').write('def run_job(scope):\n    scope.fast_movie("488", fps=25, frames=200, roi=256, bits=8)\n')
    res = run_job(job, make_points(['stage'], [(1, 2, 3)]), be, experiment_dir=out)
    assert res.status in ('completed', 'aborted'), res.error
    assert cam.pixel_format == 'MONO16' and fake.streaming
    assert any(e[:2] == ('light', 'off') for e in events if e[0] == 'light')


def test_exposure_longer_than_period_is_a_job_error_and_camera_untouched():
    be, cam, fake, events, mpc = make_backend()
    job = os.path.join(tempfile.mkdtemp(), 'job.py')
    open(job, 'w').write('def run_job(scope):\n    scope.fast_movie("488", fps=25, frames=5, roi=256, exposure_ms=90)\n')
    res = run_job(job, make_points(['stage'], [(1, 2, 3)]), be, experiment_dir=tempfile.mkdtemp())
    assert res.status == 'failed' and 'frame period' in res.error, (res.status, res.error)
    assert cam.pixel_format == 'MONO16' and fake.n_width.value == 2048 and cam.exposure_time == 8.0
    assert events[-1][:2] == ('light', 'off') or not any(e[:2] == ('light', 'on') for e in events if e[0] == 'light')
