"""Offscreen test of the Single measurement tab (control/single_measurement_widget.py) with a fake MultiPointController: the job it writes, the checks
before starting, the start sequence, the finish handling and that the JOBs tab ignores messages of its runs.

Run:  python tests/test_single_measurement_offscreen.py     (or python -m pytest tests/test_single_measurement_offscreen.py)
"""
import os
import sys
import types
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SW = os.path.dirname(HERE)
os.chdir(SW)
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path.insert(0, SW)
from qtpy.QtCore import QObject, Signal                                  # noqa: E402
from qtpy.QtWidgets import QApplication, QMessageBox                     # noqa: E402

app = QApplication.instance() or QApplication([])

import control.single_measurement_widget as smw                         # noqa: E402
from control.jobs.runner import JobResult                               # noqa: E402

popups = []
QMessageBox.warning = staticmethod(lambda *a, **k: popups.append(a[2]))


class FakeMPC(QObject):
    signal_job_message = Signal(str)
    signal_job_status = Signal(object)
    acquisitionFinished = Signal()

    def __init__(self):
        super().__init__()
        self.navigationController = types.SimpleNamespace(x_pos_mm=10.0, y_pos_mm=20.0, z_pos_mm=3.0)
        self.camera = types.SimpleNamespace(Width=3000, Height=2000, pixel_size_byte=2)
        self.experiment_ID = 'single_x'
        self.job_result = None
        self.busy = False
        self.calls = []

    def acquisition_running(self): return self.busy
    def set_base_path(self, p): self.calls.append(('base', p))
    def set_selected_configurations(self, n): self.calls.append(('channels', list(n)))
    def set_gen_focus_map_flag(self, f): self.calls.append(('focusmap', f))
    def start_new_experiment(self, i): self.calls.append(('experiment', i))
    def run_job(self, path, points, save_as='multipage'): self.calls.append(('run', path, [(p.x, p.y, p.z) for p in points], save_as, open(path).read()))
    def request_abort_aquisition(self): self.calls.append(('abort',))


class FakeConfigs:
    def __init__(self):
        mk = lambda n, e: types.SimpleNamespace(name=n, exposure_time=e, analog_gain=0.0, illumination_intensity=20.0)
        self.configurations = [mk('BF LED matrix full', 5.0), mk('Fluorescence 488 nm Ex', 8.0), mk('Fluorescence 561 nm Ex', 100.0), mk('RGB', 1.0)]


def make():
    smw.CACHE_FILE = os.path.join(tempfile.mkdtemp(), 'settings.json')
    smw.JOB_FILE = os.path.join(tempfile.mkdtemp(), 'job.py')
    popups.clear()
    mpc = FakeMPC()
    w = smw.SingleMeasurementWidget(mpc, FakeConfigs())
    w.lineEdit_savingDir.setText(tempfile.mkdtemp())
    return w, mpc


def runs(mpc):
    return [c for c in mpc.calls if c[0] == 'run']


def test_channels_exclude_unsupported_and_capture_job():
    w, mpc = make()
    assert [w.list_channels.item(i).data(smw.Qt.UserRole) for i in range(w.list_channels.count())] == ['BF LED matrix full', 'Fluorescence 488 nm Ex', 'Fluorescence 561 nm Ex']
    w.start_capture()
    assert popups and 'at least one channel' in popups[-1] and not runs(mpc)
    w.list_channels.item(0).setCheckState(smw.Qt.Checked)
    w.list_channels.item(1).setCheckState(smw.Qt.Checked)
    w.combo_save_as.setCurrentIndex(w.combo_save_as.findData('single'))
    w.start_capture()
    r = runs(mpc)[-1]
    assert r[2] == [(10.0, 20.0, 3.0)] and r[3] == 'single'
    assert "scope.capture_channels(['BF LED matrix full', 'Fluorescence 488 nm Ex'])" in r[4]
    assert ('channels', ['BF LED matrix full', 'Fluorescence 488 nm Ex']) in mpc.calls and mpc.job_owner == 'single'
    assert w.running and not w.btn_capture.isEnabled() and w.btn_abort.isEnabled()
    # a second start while running is refused
    n = len(runs(mpc))
    w.start_capture(); w.start_fast()
    assert len(runs(mpc)) == n and 'still running' in popups[-1]
    # finish
    mpc.job_result = JobResult(status='completed')
    mpc.acquisitionFinished.emit()
    assert not w.running and w.btn_capture.isEnabled() and not w.btn_abort.isEnabled() and 'completed' in w.label_status.text()


def test_fast_job_parameters_and_checks():
    w, mpc = make()
    w.combo_fast_channel.setCurrentIndex(1)           # 488, 8 ms
    w.spin_fps.setValue(25); w.spin_duration.setValue(5); w.spin_roi.setValue(2000)
    w.combo_bits.setCurrentIndex(w.combo_bits.findData(8))
    w.start_fast()
    r = runs(mpc)[-1]
    assert "scope.fast_movie('Fluorescence 488 nm Ex', fps=25.0, duration_s=5.0, roi=2000, bits=8, exposure_ms=None, gain=None, compression='none')" in r[4], r[4]
    assert ('channels', ['Fluorescence 488 nm Ex']) in mpc.calls
    mpc.job_result = JobResult(status='completed'); mpc.acquisitionFinished.emit()
    # exposure of the channel (100 ms) longer than the 40 ms frame period
    w.combo_fast_channel.setCurrentIndex(2)
    n = len(runs(mpc))
    w.start_fast()
    assert len(runs(mpc)) == n and 'shorter than the frame period' in popups[-1]
    # ... unless this movie overrides the exposure
    w.check_exposure.setChecked(True); w.spin_exposure.setValue(10); w.check_gain.setChecked(True); w.spin_gain.setValue(3)
    w.start_fast()
    r = runs(mpc)[-1]
    assert 'exposure_ms=10.0, gain=3.0' in r[4]
    mpc.job_result = JobResult(status='completed'); mpc.acquisitionFinished.emit()
    # too much memory: 16 bit, 60 s at 25 fps, 2000x2000 = 12 GB
    w.combo_bits.setCurrentIndex(w.combo_bits.findData(16)); w.spin_duration.setValue(60)
    n = len(runs(mpc))
    w.start_fast()
    assert len(runs(mpc)) == n and 'memory' in popups[-1]
    assert 'GB' in w.label_info.text()


def test_no_saving_dir_busy_and_abort_and_settings_cache():
    w, mpc = make()
    w.lineEdit_savingDir.setText('/does/not/exist')
    w.list_channels.item(0).setCheckState(smw.Qt.Checked)
    w.start_capture()
    assert 'saving directory' in popups[-1].lower() and not runs(mpc)
    w.lineEdit_savingDir.setText(tempfile.mkdtemp())
    mpc.busy = True
    w.start_capture()
    assert 'still running' in popups[-1] and not runs(mpc)
    mpc.busy = False
    w.start_capture()
    assert w.running
    w.abort()
    assert ('abort',) in mpc.calls and not w.btn_abort.isEnabled()
    mpc.acquisitionFinished.emit()
    # settings are remembered
    w.spin_fps.setValue(30); w.spin_roi.setValue(1024); w._save_cache()
    w2 = smw.SingleMeasurementWidget(FakeMPC(), FakeConfigs())
    assert w2.spin_fps.value() == 30 and w2.spin_roi.value() == 1024 and w2.selected_channels() == ['BF LED matrix full']


def test_messages_only_in_the_owner_tab():
    from control.jobs_widget import JobsWidget
    w, mpc = make()
    jw = JobsWidget(mpc, FakeConfigs(), types.SimpleNamespace(lineEdit_savingDir=types.SimpleNamespace(text=lambda: ''), location_ids=[], location_list=[]))
    mpc.job_owner = 'single'
    mpc.signal_job_message.emit('hello from single')
    assert 'hello from single' in w.log.toPlainText() and 'hello from single' not in jw.log.toPlainText()
    mpc.job_owner = 'jobs'
    mpc.signal_job_message.emit('hello from jobs')
    assert 'hello from jobs' in jw.log.toPlainText() and 'hello from jobs' not in w.log.toPlainText()


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
