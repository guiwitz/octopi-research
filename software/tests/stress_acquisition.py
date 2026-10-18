"""Stress test of starting / stopping / restarting acquisitions in SIMULATION, with realistic timing and fault injection.

Every scenario runs in its own process (a crash or hang of one does not stop the others):
    python tests/stress_acquisition.py                  # all scenarios, report at the end
    python tests/stress_acquisition.py --only S1,S3     # some
    python tests/stress_acquisition.py --scenario S1    # one, in this process (debugging)

What is made realistic (test-only patches, no change to the software):
  * the simulated microcontroller is busy for a while after every command (moves 0.25 s, other commands 10 ms),
    instead of finishing instantly;
  * the simulated camera needs exposure + 30 ms to deliver a frame;
  * faults can be injected: a camera read that raises (as the FLIR/PySpin driver does when GetNextImage(1000) times out) or returns None.
Observed: crash (exit code), hang (no 'acquisition finished' within the time limit), number of acquisition workers
running at the same time, a running QThread being replaced, illumination left on while the stage moves, stage moves /
camera triggers coming from the GUI thread while an acquisition runs, leaked threads, GUI state after the run.
"""
import os
import sys
import time
import json
import random
import argparse
import tempfile
import threading
import subprocess

SW = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCENARIOS = {}


def scenario(name, timeout_s=120):
    def deco(fn):
        SCENARIOS[name] = (fn, timeout_s)
        return fn
    return deco


# ------------------------------------------------------------------------------------------------------------------
# harness (only used inside a scenario process)
# ------------------------------------------------------------------------------------------------------------------
class H:
    pass


def setup():
    os.chdir(SW)
    sys.path.insert(0, SW)
    os.environ['QT_API'] = 'pyqt5'
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    os.environ['SQUID_SKIP_CONFIRM'] = '1'
    from unittest.mock import MagicMock
    for m in ['napari', 'napari.utils', 'napari.utils.colormaps']:
        sys.modules[m] = MagicMock()
    import control._def as d
    d.USE_NAPARI_FOR_LIVE_VIEW = d.USE_NAPARI_FOR_MULTIPOINT = d.USE_NAPARI_FOR_TILED_DISPLAY = False
    from qtpy.QtWidgets import QApplication, QMessageBox
    from qtpy.QtCore import QThread
    import control.core as core
    import control.microcontroller as mcu_mod
    import control.camera as cam_mod
    from control._def import CMD_SET

    h = H()
    h.d, h.core, h.QThread = d, core, QThread
    h.main_thread = threading.main_thread()
    h.obs = dict(max_workers=0, workers=0, replaced_running_thread=0, worker_runs=0, worker_ends=0,
                 illum_on=False, moves_with_illum_on=0, gui_moves_during_acq=0, gui_triggers_during_acq=0,
                 concurrent_camera_reads=0, max_af_threads=0, af_terminated=0, finished_signals=0, popups=[])
    h.fault = dict(read_raise_at=None, read_none_at=None, reads=0)
    MOVES = {CMD_SET.MOVE_X, CMD_SET.MOVE_Y, CMD_SET.MOVE_Z, CMD_SET.MOVETO_X, CMD_SET.MOVETO_Y, CMD_SET.MOVETO_Z}
    obs, fault = h.obs, h.fault

    # --- microcontroller: busy for a realistic time after each command, and bookkeeping
    Sim = mcu_mod.Microcontroller_Simulation
    orig_send = Sim.send_command
    lock = threading.Lock()

    def send_command(self, command):
        kind = command[1]
        if kind == 42:
            obs.setdefault('joystick_cmds', []).append(int(command[2]))
        if kind == CMD_SET.SET_STROBE_DELAY:
            obs.setdefault('strobe_cmds', []).append((command[3] << 24) + (command[4] << 16) + (command[5] << 8) + command[6])
        with lock:
            now = time.time()
            self._busy_until = max(getattr(self, '_busy_until', 0), now) + (0.25 if kind in MOVES else 0.01)
        return orig_send(self, command)
    Sim.send_command = send_command
    Sim.is_busy = lambda self: time.time() < getattr(self, '_busy_until', 0)

    # --- illumination (LiveController) and stage moves (NavigationController): bookkeeping
    for name in ('turn_on_illumination', 'turn_off_illumination'):
        orig = getattr(core.LiveController, name)

        def wrap(self, *a, _orig=orig, _on=(name == 'turn_on_illumination'), **k):
            obs['illum_on'] = _on
            return _orig(self, *a, **k)
        setattr(core.LiveController, name, wrap)
    for name in ('move_x', 'move_y', 'move_z', 'move_x_to', 'move_y_to', 'move_z_to', 'move_x_usteps', 'move_y_usteps', 'move_z_usteps'):
        orig = getattr(core.NavigationController, name, None)
        if orig is None:
            continue

        def wrapm(self, *a, _orig=orig, **k):
            if obs['illum_on']:
                obs['moves_with_illum_on'] += 1
            if obs['workers'] > 0 and threading.current_thread() is h.main_thread and not getattr(h, 'in_finish', False):
                obs['gui_moves_during_acq'] += 1
            return _orig(self, *a, **k)
        setattr(core.NavigationController, name, wrapm)

    # --- camera: exposure time + transfer, fault injection, concurrent use
    h.flir = os.environ.get('SQUID_STRESS_CAMERA', '').lower() == 'flir'
    if h.flir:
        # the real FLIR driver (control/camera_flir.py) on a fake Spinnaker SDK (tests/fake_pyspin.py)
        sys.path.insert(0, os.path.join(SW, 'tests'))
        import fake_pyspin
        sys.modules['PySpin'] = fake_pyspin
        import control.camera_flir as cf
        h.fake_sdk = fake_pyspin
        h.fake_cam = fake_pyspin.reset()
        Cam = cf.Camera
        orig_hw = Sim.send_hardware_trigger

        def send_hardware_trigger(self, *a, **k):
            r = orig_hw(self, *a, **k)
            h.fake_cam.hardware_trigger('Line2')   # the trigger line of the microcontroller goes to the camera
            return r
        Sim.send_hardware_trigger = send_hardware_trigger
    else:
        Cam = cam_mod.Camera_Simulation
    orig_read, orig_trig = Cam.read_frame, Cam.send_trigger
    inside = [0]

    def read_frame(self, *a, **k):
        inside[0] += 1
        try:
            if inside[0] > 1:
                obs['concurrent_camera_reads'] += 1
            fault['reads'] += 1
            n = fault['reads']
            if h.flir:
                fc = h.fake_cam
                if fault['read_raise_at'] is not None and n == fault['read_raise_at']:
                    with fc.lock:              # camera stops delivering: this frame and the next two are lost
                        fc.pending.clear()
                        fc.buffer.clear()
                    fc.fault['drop_triggers'] = 2
                if fault['read_none_at'] is not None and n == fault['read_none_at']:
                    fc.fault['incomplete_frames'] = 1
                img = orig_read(self)
                try:
                    cfg = h.win.liveController.currentConfiguration
                    obs.setdefault('frames', []).append((cfg.name, float(cfg.exposure_time), None if img is None else int(img[0, 1])))
                except Exception:
                    pass
                return img
            time.sleep((getattr(self, 'exposure_time', 10) or 10) / 1000.0 + 0.03)
            if fault['read_raise_at'] is not None and n == fault['read_raise_at']:
                raise RuntimeError("Spinnaker: Failed waiting for EventData on NEW_BUFFER_DATA event. [-1011]  (injected: camera timeout as in FLIR GetNextImage)")
            if fault['read_none_at'] is not None and n == fault['read_none_at']:
                return None
            return orig_read(self)
        finally:
            inside[0] -= 1
    Cam.read_frame = read_frame

    def send_trigger(self, *a, **k):
        if obs['workers'] > 0 and threading.current_thread() is h.main_thread:
            obs['gui_triggers_during_acq'] += 1
        return orig_trig(self, *a, **k)
    Cam.send_trigger = send_trigger

    # --- acquisition worker / controller bookkeeping
    orig_run = core.MultiPointWorker.run

    def run(self):
        obs['workers'] += 1
        obs['worker_runs'] += 1
        obs['max_workers'] = max(obs['max_workers'], obs['workers'])
        try:
            orig_run(self)
        finally:
            obs['workers'] -= 1
            obs['worker_ends'] += 1
    core.MultiPointWorker.run = run

    orig_start = core.MultiPointController.run_acquisition

    def run_acquisition(self, *a, **k):
        t = getattr(self, 'thread', None)
        if isinstance(t, h.QThread):
            try:
                if t.isRunning() and not getattr(self, '_acquisition_done', False):   # worker still active
                    obs['replaced_running_thread'] += 1
                    print('OBS: run_acquisition replaces a QThread that is still running', flush=True)
            except RuntimeError:
                pass
        return orig_start(self, *a, **k)
    core.MultiPointController.run_acquisition = run_acquisition

    orig_done = core.MultiPointController._on_acquisition_completed

    def on_done(self):
        h.in_finish = True
        try:
            return orig_done(self)
        finally:
            h.in_finish = False
    core.MultiPointController._on_acquisition_completed = on_done

    orig_af = core.AutoFocusController.autofocus

    def af(self, *a, **k):
        t = getattr(self, 'thread', None)
        try:
            if isinstance(t, h.QThread) and t.isRunning():
                obs['af_terminated'] += 1
                print('OBS: autofocus started while another autofocus thread runs (it will be terminate()d)', flush=True)
        except RuntimeError:
            pass
        return orig_af(self, *a, **k)
    core.AutoFocusController.autofocus = af

    QMessageBox.warning = staticmethod(lambda *a, **k: obs['popups'].append(str(a[2]) if len(a) > 2 else ''))
    QMessageBox.information = staticmethod(lambda *a, **k: obs['popups'].append(str(a[2]) if len(a) > 2 else ''))

    # never touch the user's channel_configurations.xml: the GUI saves every exposure/gain/intensity edit into it.
    # The tests use a temporary copy of the Squid1 channel list (tests/fixtures).
    import shutil
    h.channel_xml = os.path.join(tempfile.mkdtemp(prefix='stress_cfg_'), 'channel_configurations.xml')
    shutil.copy(os.path.join(SW, 'tests', 'fixtures', 'squid1_channel_configurations.xml'), h.channel_xml)
    orig_cm_init = core.ConfigurationManager.__init__

    def cm_init(self, filename='channel_configurations.xml', *a, **k):
        if os.path.basename(str(filename)) == 'channel_configurations.xml':
            filename = h.channel_xml
        return orig_cm_init(self, filename, *a, **k)
    core.ConfigurationManager.__init__ = cm_init

    import control.gui_hcs as gui
    if h.flir:
        def flir_camera(**kw):
            c = cf.Camera(sn=None, **kw)
            c.open()
            return c
        gui.camera.Camera_Simulation = flir_camera
    h.app = QApplication([])
    h.win = gui.OctopiGUI(is_simulation=True)
    h.win.show()
    h.mp, h.mpc = h.win.multiPointWidget2, h.win.multipointController
    h.mpc.acquisitionFinished.connect(lambda: obs.__setitem__('finished_signals', obs['finished_signals'] + 1))
    h.save = tempfile.mkdtemp(prefix='stress_')
    print('OBS: camera = ' + ('FLIR driver on fake PySpin' if h.flir else 'Camera_Simulation'), flush=True)
    print('OBS: sys.excepthook is ' + ('the default' if sys.excepthook is sys.__excepthook__ else 'custom: ' + repr(sys.excepthook)), flush=True)
    pump(h, 0.5)
    return h


def pump(h, s):
    end = time.time() + s
    while time.time() < end:
        h.app.processEvents()
        time.sleep(0.005)


def wait_finished(h, n_expected, limit):
    end = time.time() + limit
    while time.time() < end and h.obs['finished_signals'] < n_expected:
        pump(h, 0.02)
    return h.obs['finished_signals'] >= n_expected


def setup_flexible(h, n_pos=4, nt=1, dt=0, nz=1, af=False, channels=2, exposure_ms=None):
    mp, nav = h.mp, h.win.navigationController
    mp.clear()
    h.app.processEvents()
    for i in range(n_pos):
        nav.move_x_to(10 + 2.0 * i)
        nav.move_y_to(10)
        pump(h, 0.3)
        mp.add_location()
    mp.lineEdit_savingDir.setText(h.save)
    mp.base_path_is_set = True
    h.mpc.set_base_path(h.save)
    mp.lineEdit_experimentID.setText('stress')
    mp.entry_Nt.setValue(nt)
    mp.entry_dt.setValue(dt)
    mp.entry_NZ.setValue(nz)
    mp.entry_NX.setValue(1)
    mp.entry_NY.setValue(1)
    mp.checkbox_withAutofocus.setChecked(af)
    mp.list_configurations.clearSelection()
    for i in range(min(channels, mp.list_configurations.count())):
        mp.list_configurations.item(i).setSelected(True)
    if exposure_ms is not None:
        for c in h.mpc.configurationManager.configurations:
            c.exposure_time = exposure_ms
    h.win.recordTabWidget.setCurrentWidget(mp)
    pump(h, 0.2)


def click_start_stop(h):
    h.mp.btn_startAcquisition.click()


def gui_state(h):
    tw = h.win.recordTabWidget
    return dict(start_checked=h.mp.btn_startAcquisition.isChecked(),
                tabs_enabled=all(tw.isTabEnabled(i) for i in range(tw.count())),
                nav_enabled=h.win.navigationWidget.btn_moveX_forward.isEnabled(),
                live=h.win.liveController.is_live,
                illumination_on=h.obs['illum_on'],
                callback=h.win.camera.callback_is_enabled,
                py_threads=threading.active_count())


def report(h, verdicts, extra=None):
    o = dict(h.obs)
    o.pop('popups', None)
    out = dict(obs=o, verdicts=verdicts, extra=extra or {})
    print('RESULT ' + json.dumps(out, default=str), flush=True)
    sys.stdout.flush()
    os._exit(0)


def problem(verdicts, ok, text):
    verdicts.append(('OK' if ok else 'PROBLEM', text))


# ------------------------------------------------------------------------------------------------------------------
# scenarios
# ------------------------------------------------------------------------------------------------------------------
@scenario('S1')
def s1_restart_immediately_after_stop(h):
    """Stop in the middle of a scan and press Start again immediately (while the old run is still stopping)."""
    setup_flexible(h, n_pos=6, nt=2, dt=0)
    click_start_stop(h)
    pump(h, 1.5)
    click_start_stop(h)               # stop
    s_after_stop = gui_state(h)
    s_after_stop['old_worker_running'] = h.obs['workers'] > 0
    s_after_stop['start_usable'] = (not h.mp.btn_startAcquisition.isChecked()) and h.mp.btn_startAcquisition.isEnabled()
    pump(h, 0.05)
    click_start_stop(h)               # try to start again
    ok = wait_finished(h, 1, 60)
    pump(h, 1.0)
    v = []
    usable = s_after_stop['start_usable'] and s_after_stop['old_worker_running']
    problem(v, not usable, 'after Stop, Start is usable again before the old acquisition has ended' if usable else 'Start is locked until the old acquisition has ended')
    problem(v, h.obs['max_workers'] <= 1, f"acquisition workers running at the same time: {h.obs['max_workers']}")
    problem(v, h.obs['replaced_running_thread'] == 0, 'Start replaced a still running acquisition thread ("QThread: Destroyed while thread is still running" -> crash)' if h.obs['replaced_running_thread'] else 'no running thread was replaced')
    end = gui_state(h)
    problem(v, ok and not end['start_checked'] and h.mp.btn_startAcquisition.isEnabled(), 'the stopped run ended and Start is usable again afterwards' if ok else 'the stopped run did not end')
    report(h, v, dict(after_stop=s_after_stop, end=end))


@scenario('S2')
def s2_restart_after_finished_same_event(h):
    """Start again as soon as 'acquisition finished' arrives (thread still shutting down), 10 times."""
    setup_flexible(h, n_pos=2, nt=1, dt=0)
    from qtpy.QtCore import QTimer
    n = 10
    state = dict(started=1)

    def again():
        if state['started'] < n:
            state['started'] += 1
            QTimer.singleShot(0, lambda: click_start_stop(h))
    h.mpc.acquisitionFinished.connect(again)
    click_start_stop(h)
    ok = wait_finished(h, n, 90)
    v = []
    problem(v, ok, f"{h.obs['finished_signals']}/{n} back-to-back runs finished")
    problem(v, h.obs['replaced_running_thread'] == 0, f"a still running QThread was replaced {h.obs['replaced_running_thread']} times")
    problem(v, h.obs['max_workers'] <= 1, f"max workers at the same time: {h.obs['max_workers']}")
    report(h, v, dict(end=gui_state(h)))


@scenario('S3', timeout_s=300)
def s3_random_stop_cycles(h):
    """20 cycles: start, stop after a random time, wait for the end, check the state."""
    setup_flexible(h, n_pos=3, nt=3, dt=4, af=False)
    rnd = random.Random(1)
    v, states, bad = [], [], []
    threads0 = threading.active_count()
    for c in range(20):
        n0 = h.obs['finished_signals']
        click_start_stop(h)
        pump(h, rnd.uniform(0.1, 3.0))
        if h.obs['finished_signals'] == n0:
            click_start_stop(h)
        if not wait_finished(h, n0 + 1, 30):
            bad.append(f'cycle {c}: no end within 30 s')
            break
        pump(h, 0.3)
        s = gui_state(h)
        states.append(s)
        if s['start_checked'] or not s['tabs_enabled'] or not s['nav_enabled'] or s['illumination_on']:
            bad.append(f'cycle {c}: {s}')
    problem(v, not bad, 'GUI/hardware state after every stop: ' + ('; '.join(bad[:3]) if bad else 'clean'))
    leaked = threading.active_count() - threads0
    problem(v, leaked <= 1, f'Python threads: {threads0} before, {threading.active_count()} after 20 cycles (leak {leaked})')
    problem(v, h.obs['max_workers'] <= 1, f"max workers: {h.obs['max_workers']}")
    report(h, v, dict(last=states[-1] if states else None))


@scenario('S4')
def s4_live_on_then_cycles(h):
    """Live view on, start, stop, 5 times: live must come back each time; no camera trigger from the GUI during the run."""
    setup_flexible(h, n_pos=3, nt=2, dt=0)
    v, bad = [], []
    for c in range(5):
        h.win.liveControlWidget.btn_live.setChecked(True)
        h.win.liveControlWidget.toggle_live(True)
        pump(h, 0.5)
        n0 = h.obs['finished_signals']
        click_start_stop(h)
        pump(h, 1.0)
        click_start_stop(h)
        if not wait_finished(h, n0 + 1, 30):
            bad.append(f'cycle {c}: no end')
            break
        pump(h, 0.5)
        if not h.win.liveController.is_live:
            bad.append(f'cycle {c}: live not restored')
        h.win.liveControlWidget.btn_live.setChecked(False)
        h.win.liveControlWidget.toggle_live(False)
        pump(h, 0.2)
    problem(v, not bad, 'live restored after each stop: ' + ('; '.join(bad) if bad else 'yes'))
    problem(v, h.obs['gui_triggers_during_acq'] == 0, f"camera triggers sent by the GUI (live) while the acquisition ran: {h.obs['gui_triggers_during_acq']}")
    report(h, v, dict(end=gui_state(h)))


@scenario('S5')
def s5_camera_read_raises(h):
    """The camera read fails once (FLIR: GetNextImage(1000) times out -> SpinnakerException)."""
    setup_flexible(h, n_pos=3, nt=1)
    h.fault['read_raise_at'] = h.fault['reads'] + 3
    click_start_stop(h)
    ok = wait_finished(h, 1, 20)
    v = []
    problem(v, ok, "acquisition ended normally after a camera read error" if ok else "after a camera read error the acquisition never ends: GUI stays in the running state" + (", illumination stays ON" if h.obs['illum_on'] else ''))
    report(h, v, dict(end=gui_state(h)))


@scenario('S5b')
def s5b_camera_error_then_stop_and_start(h):
    """After a camera read error: is the GUI released (or does the user have to press Stop), and can a new run start?"""
    setup_flexible(h, n_pos=3, nt=1)
    h.fault['read_raise_at'] = h.fault['reads'] + 3
    click_start_stop(h)
    wait_finished(h, 1, 10)
    pump(h, 1.0)
    after_error = gui_state(h)
    err = getattr(h.mpc, 'last_acquisition_error', None)
    stuck = after_error['start_checked']
    if stuck:
        click_start_stop(h)        # what a user does: Stop ...
        pump(h, 3)
    n0 = h.obs['finished_signals']
    click_start_stop(h)            # ... and Start again
    ok = wait_finished(h, n0 + 1, 30)
    v = []
    problem(v, not stuck, 'GUI left in the "running" state after the camera error' if stuck else 'GUI released after the camera error (no Stop needed)')
    problem(v, not after_error['illumination_on'], 'illumination left ON after the camera error' if after_error['illumination_on'] else 'illumination off after the camera error')
    problem(v, bool(err), 'the user is told why the acquisition stopped' if err else 'no message to the user')
    problem(v, ok, 'a new acquisition can be started and finishes' if ok else 'a new acquisition could not be run')
    report(h, v, dict(after_error=after_error, error=err))


@scenario('S6')
def s6_camera_read_returns_none(h):
    """The camera returns no image once (other drivers / simulated timeout): the run continues - is the light switched off?"""
    setup_flexible(h, n_pos=3, nt=1, channels=1)
    h.fault['read_none_at'] = h.fault['reads'] + 1
    m0 = h.obs['moves_with_illum_on']
    click_start_stop(h)
    ok = wait_finished(h, 1, 30)
    v = []
    problem(v, ok, 'run ended')
    problem(v, h.obs['moves_with_illum_on'] == m0, f"stage moves while the illumination was still on: {h.obs['moves_with_illum_on'] - m0}")
    report(h, v, dict(end=gui_state(h)))


@scenario('S7')
def s7_camera_error_during_autofocus(h):
    """Camera read fails during an autofocus of the acquisition."""
    setup_flexible(h, n_pos=2, nt=1, af=True)
    h.win.autofocusController.set_af_schedule('every_timepoint', 1, False)
    h.fault['read_raise_at'] = h.fault['reads'] + 2
    click_start_stop(h)
    ok = wait_finished(h, 1, 30)
    v = []
    problem(v, ok, 'acquisition ended after a camera error inside the autofocus' if ok else 'after a camera error inside the autofocus the acquisition never ends (waits forever for the autofocus)' + (', illumination stays ON' if h.obs['illum_on'] else ''))
    report(h, v, dict(end=gui_state(h)))


@scenario('S8')
def s8_camera_none_during_autofocus(h):
    """Camera returns no image during an autofocus: illumination left on while z moves?"""
    setup_flexible(h, n_pos=2, nt=1, af=True, channels=1)
    h.win.autofocusController.set_af_schedule('every_timepoint', 1, False)
    h.fault['read_none_at'] = h.fault['reads'] + 2
    m0 = h.obs['moves_with_illum_on']
    click_start_stop(h)
    ok = wait_finished(h, 1, 40)
    v = []
    problem(v, ok, 'run ended')
    problem(v, h.obs['moves_with_illum_on'] == m0, f"z moves while the illumination was on: {h.obs['moves_with_illum_on'] - m0}")
    report(h, v, dict(end=gui_state(h)))


@scenario('S9')
def s9_stop_during_autofocus(h):
    """Stop while an autofocus runs: how long until the acquisition really ends?"""
    setup_flexible(h, n_pos=3, nt=1, af=True)
    h.win.autofocusController.set_af_schedule('every_timepoint', 1, False)
    af = h.win.autofocusController
    click_start_stop(h)
    t_end = time.time() + 15
    while time.time() < t_end and not af.autofocus_in_progress:
        pump(h, 0.01)
    t0 = time.time()
    click_start_stop(h)
    ok = wait_finished(h, 1, 30)
    v = []
    problem(v, ok, f'ended {time.time() - t0:.1f} s after Stop (pressed during autofocus)')
    report(h, v, dict(stop_to_end_s=round(time.time() - t0, 2), end=gui_state(h)))


@scenario('S10')
def s10_user_actions_during_acquisition(h):
    """While an acquisition runs, a user clicks / types / scrolls (real Qt input events): Live, Autofocus, positions
    Next (button and Alt Gr + arrow), exposure field, Positions window. Nothing may reach the hardware; Stop must work."""
    from qtpy.QtTest import QTest
    from qtpy.QtCore import Qt, QPoint
    from qtpy.QtGui import QWheelEvent
    setup_flexible(h, n_pos=4, nt=3, dt=0)
    mp, win = h.mp, h.win
    if hasattr(mp, 'checkbox_move_to_selected'):
        mp.checkbox_move_to_selected.setChecked(True)
    win.activateWindow()
    click_start_stop(h)
    pump(h, 1.0)
    g0, t0 = h.obs['gui_moves_during_acq'], h.obs['gui_triggers_during_acq']
    live_before = win.liveController.is_live
    # mouse clicks
    QTest.mouseClick(win.liveControlWidget.btn_live, Qt.LeftButton); pump(h, 0.8)
    QTest.mouseClick(win.autofocusWidget.btn_autofocus, Qt.LeftButton); pump(h, 0.5)
    for name in ('btn_next', 'btn_next_location', 'btn_next_position'):
        b = getattr(mp, name, None) or getattr(getattr(mp, 'towbin_widget', None), name, None)
        if b is not None:
            QTest.mouseClick(b, Qt.LeftButton); pump(h, 0.3)
    # keyboard shortcut and typing
    QTest.keyClick(win, Qt.Key_Right, Qt.ControlModifier | Qt.AltModifier); pump(h, 0.3)
    QTest.keyClick(mp.table_location_list, Qt.Key_Delete); pump(h, 0.3)
    n_positions = len(mp.location_list)
    exp = win.liveControlWidget.entry_exposureTime
    v0 = exp.value()
    exp.setFocus(); QTest.keyClick(exp, Qt.Key_Up); pump(h, 0.2)
    exp_changed = exp.value() != v0
    v = []
    problem(v, win.liveController.is_live == live_before, 'Live button click during the acquisition ' + ('was ignored' if win.liveController.is_live == live_before else 'STARTED LIVE'))
    problem(v, h.obs['gui_moves_during_acq'] == g0, f"stage moves from the GUI during the acquisition: {h.obs['gui_moves_during_acq'] - g0}")
    problem(v, h.obs['gui_triggers_during_acq'] == t0, f"camera triggers from the GUI during the acquisition: {h.obs['gui_triggers_during_acq'] - t0}")
    problem(v, h.obs['concurrent_camera_reads'] == 0, f"camera read by two threads at the same time: {h.obs['concurrent_camera_reads']}")
    problem(v, not exp_changed, 'exposure field ' + ('CHANGED by the keyboard' if exp_changed else 'could not be changed'))
    problem(v, n_positions == 4, f'positions after pressing Delete in the table: {n_positions} (4 expected)')
    # Stop with a real mouse click must still work
    QTest.mouseClick(mp.btn_startAcquisition, Qt.LeftButton)
    ok = wait_finished(h, 1, 40)
    problem(v, ok, 'Stop (real mouse click) ended the acquisition' if ok else 'Stop click did not end the acquisition')
    pump(h, 0.5)
    exp.setFocus(); QTest.keyClick(exp, Qt.Key_Up); pump(h, 0.2)
    problem(v, exp.value() != v0, 'after the acquisition the controls work again' if exp.value() != v0 else 'controls still locked after the acquisition')
    lock = getattr(win, 'inputLock', None)
    report(h, v, dict(blocked_inputs=getattr(lock, 'blocked_count', None), end=gui_state(h)))


@scenario('S11')
def s11_long_exposure(h):
    """Exposure >= ~1 s: does the camera driver wait long enough for the frame? (source check; PySpin is not installed here)"""
    v = []
    src = open(os.path.join(SW, 'control', 'camera_flir.py'), encoding='utf-8', errors='replace').read()
    fixed = 'GetNextImage(1000)' in src
    problem(v, not fixed, 'FLIR read_frame waits a fixed 1000 ms (GetNextImage(1000)): exposures of about 1 s or more always time out -> exception -> stuck acquisition' if fixed else 'FLIR read_frame timeout depends on the exposure')
    zeros = 'np.zeros((self.Width,self.Height))' in src
    problem(v, not zeros, 'FLIR read_frame returns np.zeros((Width, Height)) for an incomplete frame: black float64 image with swapped axes is processed and saved as if it were data' if zeros else 'incomplete frames handled')
    report(h, v)


@scenario('S12')
def s12_close_during_acquisition(h):
    """Close the main window while an acquisition runs."""
    setup_flexible(h, n_pos=4, nt=3, dt=0)
    click_start_stop(h)
    pump(h, 1.0)
    t0 = time.time()
    h.win.close()
    pump(h, 3.0)
    non_daemon = [t.name for t in threading.enumerate() if t is not threading.main_thread() and not t.daemon and t.is_alive()]
    v = []
    problem(v, not non_daemon, f'threads that keep the Python process alive after the window is closed: {non_daemon}')
    problem(v, h.obs['workers'] == 0, f"acquisition worker still running after close: {h.obs['workers']}")
    report(h, v, dict(close_s=round(time.time() - t0, 2)))


@scenario('S13')
def s13_job_stop_restart(h):
    """JOBs: start a job, abort, start again immediately."""
    mp, jw = h.mp, h.win.jobsWidget
    setup_flexible(h, n_pos=3, nt=1)
    job = os.path.join(h.save, 'j.py')
    open(job, 'w').write('def run_job(scope):\n    for t in scope.timepoints(50, interval=1):\n        for p in scope.points():\n            scope.move_to(p)\n            scope.capture(scope.channels[0])\n')
    h.win.recordTabWidget.setCurrentWidget(jw)
    jw.lineEdit_job.setText(job)
    jw.lineEdit_savingDir.setText(h.save)
    jw.btn_start.click()
    pump(h, 2.0)
    jw.btn_start.click()       # abort
    s = dict(running_flag=jw.running, button_checked=jw.btn_start.isChecked(), button_enabled=jw.btn_start.isEnabled())
    pump(h, 0.05)
    jw.btn_start.click()       # start again
    pump(h, 0.5)
    restarted = jw.running and h.obs['worker_runs'] >= 2
    if jw.running:
        jw.btn_start.click()
    wait_finished(h, 2 if restarted else 1, 40)
    v = []
    problem(v, h.obs['max_workers'] <= 1, f"JOBs: workers at the same time after abort + immediate start: {h.obs['max_workers']}")
    problem(v, h.obs['replaced_running_thread'] == 0, 'JOBs: Start replaced a running acquisition thread' if h.obs['replaced_running_thread'] else 'JOBs: Start is locked until the aborted job has ended')
    report(h, v, dict(after_abort=s, restarted_while_old_running=restarted))


@scenario('S14')
def s14_job_pause_unlocks_input(h):
    """JOBs: the controls are locked while the job runs, usable while it is paused, locked again after resume."""
    from qtpy.QtTest import QTest
    from qtpy.QtCore import Qt
    mp, jw, win = h.mp, h.win.jobsWidget, h.win
    setup_flexible(h, n_pos=2, nt=1)
    job = os.path.join(h.save, 'jp.py')
    open(job, 'w').write('def run_job(scope):\n    for t in scope.timepoints(200, interval=1):\n        for p in scope.points():\n            scope.move_to(p)\n            scope.capture(scope.channels[0])\n')
    win.recordTabWidget.setCurrentWidget(jw)
    jw.lineEdit_job.setText(job)
    jw.lineEdit_savingDir.setText(h.save)
    QTest.mouseClick(jw.btn_start, Qt.LeftButton)
    pump(h, 2.0)
    exp = win.liveControlWidget.entry_exposureTime

    def try_change():
        v0 = exp.value()
        exp.setFocus(); QTest.keyClick(exp, Qt.Key_Up); pump(h, 0.2)
        changed = exp.value() != v0
        exp.setValue(v0)
        return changed
    running_change = try_change()
    QTest.mouseClick(jw.btn_pause, Qt.LeftButton)
    t_end = time.time() + 20
    while time.time() < t_end and not jw.paused:
        pump(h, 0.1)
    paused_change = try_change() if jw.paused else None
    QTest.mouseClick(jw.btn_pause, Qt.LeftButton)        # resume
    pump(h, 1.0)
    resumed_change = try_change()
    QTest.mouseClick(jw.btn_start, Qt.LeftButton)        # abort (confirmation skipped in tests)
    ok = wait_finished(h, 1, 40)
    v = []
    problem(v, not running_change, 'while the job runs the exposure field ' + ('CAN be changed' if running_change else 'is locked'))
    problem(v, paused_change is True, 'while the job is paused the exposure field ' + ('can be changed' if paused_change else 'is NOT usable' if paused_change is False else '(job did not pause)'))
    problem(v, not resumed_change, 'after resume the exposure field ' + ('CAN be changed' if resumed_change else 'is locked again'))
    problem(v, ok, 'job aborted with a real click on Abort' if ok else 'Abort click did not end the job')
    report(h, v)


@scenario('S15')
def s15_joystick_disabled_during_acquisition(h):
    """The microcontroller is told to ignore joystick and focus wheel during an acquisition (towbinlab firmware command 42)."""
    setup_flexible(h, n_pos=2, nt=1)
    h.obs['joystick_cmds'] = []
    click_start_stop(h)
    pump(h, 0.3)
    during = list(h.obs['joystick_cmds'])
    ok = wait_finished(h, 1, 30)
    pump(h, 0.3)
    after = list(h.obs['joystick_cmds'])
    v = []
    problem(v, during[:1] == [0], 'joystick disabled at the start: ' + str(during))
    problem(v, ok and after[-1:] == [1], 'joystick enabled again at the end: ' + str(after))
    report(h, v)


@scenario('S16')
def s16_flir_each_image_has_its_channel_exposure(h):
    """FLIR driver: in a multi-channel acquisition every image comes with the exposure of its own channel
    (the frame carries the exposure the camera had when it was triggered)."""
    v = []
    if not h.flir:
        problem(v, True, 'only meaningful with --camera flir (skipped)')
        report(h, v)
    setup_flexible(h, n_pos=2, nt=2, channels=3)
    cfgs = [h.mpc.configurationManager.configurations[i] for i in range(3)]
    live = h.win.liveControlWidget
    for c, e in zip(cfgs, (12, 22, 100)):         # set the exposures as a user does: select the channel, type the value
        live.dropdown_modeSelection.setCurrentText(c.name)
        live.entry_exposureTime.setValue(e)
    pump(h, 0.2)
    h.obs['frames'] = []
    click_start_stop(h)
    ok = wait_finished(h, 1, 60)
    frames = [f for f in h.obs['frames'] if f[0] in [c.name for c in cfgs]]
    wrong = [f for f in frames if f[2] is None or abs(f[2] - round(f[1])) > 0]
    problem(v, ok, 'acquisition finished')
    problem(v, len(frames) == 2 * 2 * 3, f'{len(frames)} images (12 expected)')
    problem(v, not wrong, 'every image has the exposure of its channel' if not wrong else f'{len(wrong)} images with a wrong exposure, e.g. {wrong[:3]} (channel, set ms, image ms)')
    report(h, v, dict(first=frames[:6]))


@scenario('S17')
def s17_flir_hardware_trigger_timing(h):
    """FLIR, hardware trigger: the microcontroller gets the camera's strobe delay (measured rolling-shutter readout),
    also after the pixel format changes; a hardware-triggered acquisition delivers its images."""
    from control._def import TriggerMode
    v = []
    if not h.flir:
        problem(v, True, 'only meaningful with --camera flir (skipped)')
        report(h, v)
    cam, live = h.win.camera, h.win.liveController
    h.obs['strobe_cmds'] = []
    live.set_trigger_mode(TriggerMode.HARDWARE)
    first = list(h.obs['strobe_cmds'])
    problem(v, first and first[-1] == int(cam.strobe_delay_us), f'strobe delay sent when switching to hardware trigger: {first} (camera {int(cam.strobe_delay_us)} us)')
    old_constant = int(cam.exposure_delay_us + 10 * cam.pixel_size_byte * (3036 - 1))
    problem(v, cam.readout_us is not None and cam.strobe_delay_us < old_constant, f'measured strobe delay {cam.strobe_delay_us / 1000:.1f} ms (IMX226 constants would give {old_constant / 1000:.1f} ms)')
    before = int(cam.strobe_delay_us)
    cam.set_pixel_format('MONO8' if cam.pixel_format != 'MONO8' else 'MONO16')
    live.set_microscope_mode(live.currentConfiguration)
    after = int(cam.strobe_delay_us)
    problem(v, after != before and h.obs['strobe_cmds'][-1:] == [after], f'after a pixel format change the new strobe delay is sent: {before} -> {after} us, sent {h.obs["strobe_cmds"]}')
    setup_flexible(h, n_pos=2, nt=1, channels=2)
    h.obs['frames'] = []
    click_start_stop(h)
    ok = wait_finished(h, 1, 60)
    frames = h.obs['frames']
    problem(v, ok and len(frames) == 4 and all(f[2] is not None for f in frames), f'hardware-triggered acquisition: {len(frames)} images, {sum(f[2] is None for f in frames)} missing')
    live.set_trigger_mode(TriggerMode.SOFTWARE)
    report(h, v, dict(frames=frames))


# ------------------------------------------------------------------------------------------------------------------
def run_one_inprocess(name):
    fn, _ = SCENARIOS[name]
    h = setup()
    try:
        fn(h)
    except Exception:
        import traceback
        traceback.print_exc()
        print('RESULT ' + json.dumps(dict(obs=h.obs, verdicts=[('PROBLEM', 'scenario raised: ' + traceback.format_exc().splitlines()[-1])], extra={}), default=str), flush=True)
        os._exit(0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--scenario')
    ap.add_argument('--only')
    ap.add_argument('--out', default=None)
    ap.add_argument('--camera', choices=['sim', 'flir'], default=None,
                    help="flir: run the real FLIR driver (control/camera_flir.py) on the fake Spinnaker SDK tests/fake_pyspin.py")
    a = ap.parse_args()
    if a.camera:
        os.environ['SQUID_STRESS_CAMERA'] = a.camera
    if a.scenario:
        run_one_inprocess(a.scenario)
        return
    names = a.only.split(',') if a.only else list(SCENARIOS)
    lines = []
    for n in names:
        fn, limit = SCENARIOS[n]
        doc = (fn.__doc__ or '').strip().splitlines()[0]
        t0 = time.time()
        try:
            p = subprocess.run([sys.executable, os.path.abspath(__file__), '--scenario', n], capture_output=True, text=True, timeout=limit)
            out, code = p.stdout + p.stderr, p.returncode
        except subprocess.TimeoutExpired as e:
            out, code = ((e.stdout or b'').decode(errors='replace') if isinstance(e.stdout, bytes) else (e.stdout or '')), 'TIMEOUT'
        res = [l for l in out.splitlines() if l.startswith('RESULT ')]
        lines.append(f'\n=== {n}: {doc}  ({time.time() - t0:.0f} s)')
        if code == 'TIMEOUT':
            lines.append(f'  PROBLEM  HANG: no result within {limit} s')
        elif not res:
            tail = [l for l in out.splitlines() if 'Error' in l or 'QThread' in l or 'Traceback' in l or 'Aborted' in l or 'Fatal' in l][-4:]
            lines.append(f'  PROBLEM  CRASH: the program died (exit code {code}). Last messages: ' + ' | '.join(tail))
        else:
            r = json.loads(res[-1][7:])
            for verdict, text in r['verdicts']:
                lines.append(f'  {verdict:8s} {text}')
            if r.get('extra'):
                lines.append('           details: ' + json.dumps(r['extra'], default=str)[:400])
        for l in out.splitlines():
            if l.startswith('OBS:'):
                lines.append('           ' + l)
        print('\n'.join(lines[-8:]), flush=True)
    text = '\n'.join(lines)
    out = a.out or os.path.join(tempfile.gettempdir(), 'stress_acquisition_report.txt')
    open(out, 'w').write(text + '\n')
    print('\nreport:', out)


if __name__ == '__main__':
    main()
