"""Stress test of starting / stopping / restarting acquisitions in SIMULATION, with realistic timing and fault injection.

Every scenario runs in its own process (a crash or hang of one does not stop the others):
    python tests/stress_acquisition.py                  # all scenarios, report at the end
    python tests/stress_acquisition.py --only S1,S3     # some
    python tests/stress_acquisition.py --scenario S1    # one, in this process (debugging)

What is made realistic (test-only patches, no change to the software):
  * the simulated microcontroller is busy for a while after every command (moves 0.25 s, other commands 10 ms),
    instead of finishing instantly;
  * the simulated camera needs exposure + 30 ms to deliver a frame;
  * faults can be injected: a camera read that raises (as the Daheng driver does on a 1 s timeout) or returns None.
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
    Cam = cam_mod.Camera_Simulation
    orig_read, orig_trig = Cam.read_frame, Cam.send_trigger
    inside = [0]

    def read_frame(self, *a, **k):
        inside[0] += 1
        try:
            if inside[0] > 1:
                obs['concurrent_camera_reads'] += 1
            time.sleep((getattr(self, 'exposure_time', 10) or 10) / 1000.0 + 0.03)
            fault['reads'] += 1
            n = fault['reads']
            if fault['read_raise_at'] is not None and n == fault['read_raise_at']:
                raise AttributeError("'NoneType' object has no attribute 'get_numpy_array'  (injected: camera timeout as in gxipy)")
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
                if t.isRunning():
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

    import control.gui_hcs as gui
    h.app = QApplication([])
    h.win = gui.OctopiGUI(is_simulation=True)
    h.win.show()
    h.mp, h.mpc = h.win.multiPointWidget2, h.win.multipointController
    h.mpc.acquisitionFinished.connect(lambda: obs.__setitem__('finished_signals', obs['finished_signals'] + 1))
    h.save = tempfile.mkdtemp(prefix='stress_')
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
    """Stop in the middle of a scan and press Start again immediately (the button is usable at once)."""
    setup_flexible(h, n_pos=6, nt=2, dt=0)
    click_start_stop(h)
    pump(h, 1.5)
    click_start_stop(h)               # stop
    s_after_stop = gui_state(h)
    s_after_stop['old_worker_running'] = h.obs['workers'] > 0
    s_after_stop['start_usable'] = (not h.mp.btn_startAcquisition.isChecked()) and h.mp.btn_startAcquisition.isEnabled()
    pump(h, 0.05)
    click_start_stop(h)               # start again
    ok = wait_finished(h, 2, 60)
    v = []
    problem(v, not (s_after_stop['start_usable'] and s_after_stop['old_worker_running']), 'after Stop, Start is usable again before the old acquisition has ended' if (s_after_stop['start_usable'] and s_after_stop['old_worker_running']) else 'Start is blocked until the old acquisition has ended')
    problem(v, h.obs['max_workers'] <= 1, f"acquisition workers running at the same time: {h.obs['max_workers']}")
    problem(v, h.obs['replaced_running_thread'] == 0, 'Start replaced a still running acquisition thread ("QThread: Destroyed while thread is still running" -> crash)' if h.obs['replaced_running_thread'] else 'no running thread was replaced')
    problem(v, ok, 'both runs ended')
    report(h, v, dict(after_stop=s_after_stop, end=gui_state(h)))


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
    """The camera read fails once (Daheng driver: get_image() times out -> None -> AttributeError)."""
    setup_flexible(h, n_pos=3, nt=1)
    h.fault['read_raise_at'] = h.fault['reads'] + 3
    click_start_stop(h)
    ok = wait_finished(h, 1, 20)
    v = []
    problem(v, ok, "acquisition ended normally after a camera read error" if ok else "after a camera read error the acquisition never ends: GUI stays in the running state" + (", illumination stays ON" if h.obs['illum_on'] else ''))
    report(h, v, dict(end=gui_state(h)))


@scenario('S5b')
def s5b_camera_error_then_stop_and_start(h):
    """After a camera read error the user presses Stop, then Start again (what one does when the GUI seems stuck)."""
    setup_flexible(h, n_pos=3, nt=1)
    h.fault['read_raise_at'] = h.fault['reads'] + 3
    click_start_stop(h)
    pump(h, 6)
    stuck = gui_state(h)
    click_start_stop(h)            # Stop
    pump(h, 3)
    after_stop = gui_state(h)
    click_start_stop(h)            # Start again
    ok = wait_finished(h, 1, 30)
    v = []
    problem(v, not stuck['start_checked'], 'GUI left in the "running" state after the camera error (locked tabs/stage, live without images)')
    problem(v, not stuck['illumination_on'], 'illumination left ON after the camera error')
    problem(v, after_stop['tabs_enabled'], 'Stop does not unlock the GUI (the "finished" signal never comes)')
    problem(v, ok, 'a new acquisition can be started and finishes')
    report(h, v, dict(stuck=stuck, after_stop=after_stop))


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
    """While an acquisition runs: are the Autofocus button, the Live button, the positions Next/Previous (Alt Gr + arrow)
    and the Positions window still usable, and do they move the stage / trigger the camera from the GUI thread?"""
    setup_flexible(h, n_pos=4, nt=3, dt=0)
    mp, win = h.mp, h.win
    mp.checkbox_move_to_selected.setChecked(True) if hasattr(mp, 'checkbox_move_to_selected') else None
    click_start_stop(h)
    pump(h, 1.0)
    enabled = dict(af_button=win.autofocusWidget.btn_autofocus.isEnabled() and win.autofocusWidget.btn_autofocus.isVisibleTo(win),
                   live_button=win.liveControlWidget.btn_live.isEnabled(),
                   flexible_tab_enabled=win.recordTabWidget.isTabEnabled(win.recordTabWidget.indexOf(mp)),
                   positions_window_next=mp.towbin_widget.isEnabled() if hasattr(mp, 'towbin_widget') else None)
    g0, af0 = h.obs['gui_moves_during_acq'], h.obs['af_terminated']
    mp.next(); pump(h, 0.3); mp.previous(); pump(h, 0.3)
    moves_next = h.obs['gui_moves_during_acq'] - g0
    win.autofocusWidget.btn_autofocus.click(); pump(h, 1.5)
    win.liveControlWidget.btn_live.click(); pump(h, 1.0); win.liveControlWidget.btn_live.click(); pump(h, 0.2)
    click_start_stop(h)
    wait_finished(h, 1, 40)
    v = []
    problem(v, moves_next == 0, f'Next/Previous (Alt Gr + arrow, Positions window) moved the stage during the acquisition: {moves_next} move commands from the GUI')
    problem(v, not enabled['af_button'], 'Autofocus button ' + ('USABLE' if enabled['af_button'] else 'locked') + ' during the acquisition')
    problem(v, not enabled['live_button'], 'Live button ' + ('USABLE during the acquisition: live triggers the camera and switches the illumination in parallel to the acquisition' if enabled['live_button'] else 'locked during the acquisition'))
    problem(v, h.obs['concurrent_camera_reads'] == 0, f"camera read by two threads at the same time: {h.obs['concurrent_camera_reads']}")
    problem(v, h.obs['gui_triggers_during_acq'] == 0, f"camera triggers from the GUI thread during the acquisition: {h.obs['gui_triggers_during_acq']}")
    report(h, v, dict(enabled=enabled, end=gui_state(h)))


@scenario('S11')
def s11_long_exposure(h):
    """Exposure 1500 ms (longer than the Daheng get_image() default timeout of 1000 ms)."""
    import control.camera as cam_mod
    import inspect
    src = inspect.getsource(cam_mod.Camera.read_frame)
    v = []
    problem(v, 'timeout' in src, 'Daheng Camera.read_frame passes a timeout to get_image() that covers the exposure time (default is 1000 ms)')
    report(h, v, dict(read_frame_source=src.strip().splitlines()[1].strip()))


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
    problem(v, h.obs['replaced_running_thread'] == 0, 'JOBs: Start replaced a running acquisition thread')
    report(h, v, dict(after_abort=s, restarted_while_old_running=restarted))


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
    a = ap.parse_args()
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
