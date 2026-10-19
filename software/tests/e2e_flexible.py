"""End-to-end test of the Flexible Multipoint improvements (simulation).

Starts the real GUI, defines positions, runs real acquisitions with the real MultiPointWorker and checks

  A. position_log.csv (xyz of every position before / after every autofocus) and that the focused z of a position is
     stored in the positions list, shown in the Positions table and used at the next time point;
  B. the same with a schedule that skips the autofocus at some time points (af_skipped rows, z carried over);
  C. the countdown / progress view of the Flexible Multipoint tab (countdown while waiting, images counted, completed);
  D. a single autofocus test (Autofocus button / joystick) uses the autofocus channel of the Contrast AF tab, not the live
     channel, and puts the live channel back afterwards;
  E. autofocus in hardware-trigger mode takes its images (it crashed before the trigger was sent: no light).

The focus measure is replaced by a function of the stage z with a known maximum (a different one for every position and
time point), so the autofocus result is known and a wrong or missing z update is detected.

  SQUID_E2E_OFFSCREEN=1 python tests/e2e_flexible.py --simulation --save-dir /tmp/squid_flex
Exit code 0 = all checks passed.
"""
import os
import sys
import csv
import glob
import time
import argparse
import tempfile
import traceback

SOFTWARE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--simulation', action='store_true', required=True)
    ap.add_argument('--save-dir', default=None)
    ap.add_argument('--dt', type=float, default=14.0, help='time between time points in scenario A (s)')
    ap.add_argument('--timeout-s', type=float, default=300)
    ap.add_argument('--scenarios', default='ABCDE')
    args = ap.parse_args()
    save_dir = args.save_dir or tempfile.mkdtemp(prefix='flex_e2e_')
    os.makedirs(save_dir, exist_ok=True)

    os.chdir(SOFTWARE_DIR)
    sys.path.insert(0, SOFTWARE_DIR)
    os.environ['QT_API'] = 'pyqt5'
    os.environ['SQUID_SKIP_CONFIRM'] = '1'
    if os.environ.get('SQUID_E2E_OFFSCREEN'):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from unittest.mock import MagicMock
        for m in ['napari', 'napari.utils', 'napari.utils.colormaps']:
            sys.modules[m] = MagicMock()
        import control._def as d
        d.USE_NAPARI_FOR_LIVE_VIEW = d.USE_NAPARI_FOR_MULTIPOINT = d.USE_NAPARI_FOR_TILED_DISPLAY = False
    from qtpy.QtCore import QEventLoop, QTimer, Qt
    from qtpy.QtWidgets import QApplication, QMessageBox
    import control.gui_hcs as gui
    from control._def import TriggerMode

    popups = []
    for fn in ('warning', 'information', 'critical'):
        setattr(QMessageBox, fn, staticmethod(lambda *a, **k: popups.append(str(a[2]) if len(a) > 2 else '')))

    app = QApplication([])
    app.setStyle('Fusion')
    win = gui.OctopiGUI(is_simulation=True)
    win.show()
    app.processEvents()
    nav, mp, mpc, afc = win.navigationController, win.multiPointWidget2, win.multipointController, win.autofocusController
    live, liveC = win.liveControlWidget, win.liveController

    def pump(seconds):
        end = time.time() + seconds
        while time.time() < end:
            app.processEvents()
            time.sleep(0.01)

    def refresh_position():
        nav.microcontroller.wait_till_operation_is_completed()
        nav.update_pos(nav.microcontroller)
        app.processEvents()

    def move_to(x, y, z):
        nav.move_x_to(x); nav.move_y_to(y); nav.move_z_to(z)
        pump(0.2)
        refresh_position()

    results = []

    def record(scn, name, ok, detail=''):
        results.append((scn, name, bool(ok), detail))
        print(('  PASS  ' if ok else '  FAIL  ') + name + (f'   [{detail}]' if detail and not ok else ''), flush=True)

    refresh_position()
    channels = [c.name for c in win.configurationManager.configurations
                if not any(u in c.name for u in ('RGB', 'USB Spectrometer'))]
    ch0, ch1 = channels[0], channels[1]
    start_x, start_y, start_z = nav.x_pos_mm, nav.y_pos_mm, 1.0
    spacing = 0.5
    print('=' * 70)
    print('simulation; channels', ch0, '/', ch1, '; saving in', save_dir)
    print('=' * 70, flush=True)

    # ---- focus measure with a known maximum (peak depends on position and time point)
    DZ_UM, N_PLANES = 1.5, 7
    afc.set_deltaZ(DZ_UM)
    afc.set_N(N_PLANES)
    afc.set_af_channel(ch1)
    orig_measure = afc.compute_focus_measure
    measure_log = []          # (channel name used for the image, z um)

    def peak_um(p, tp):
        return start_z * 1000 + 1.0 * (1 + p) + 0.5 * tp

    def fake_measure(image, name=None):
        z_um = nav.z_pos_mm * 1000
        worker = getattr(mpc, 'multiPointWorker', None)
        tp = worker.time_point if (worker is not None and mpc.acquisition_running()) else 0
        p = int(round((nav.x_pos_mm - start_x) / spacing))
        cfg = liveC.currentConfiguration
        measure_log.append((cfg.name if cfg is not None else None, z_um))
        return 1.0 / (1.0 + ((z_um - peak_um(p, tp)) / 1.5) ** 2)

    afc.compute_focus_measure = fake_measure

    def read_log(exp_dir):
        path = os.path.join(exp_dir, 'position_log.csv')
        if not os.path.isfile(path):
            return None
        with open(path, newline='', encoding='utf-8') as f:
            return list(csv.DictReader(f))

    def find_exp(exp_id):
        c = [d for d in glob.glob(os.path.join(save_dir, exp_id + '_*')) if os.path.isdir(d)]
        return max(c, key=os.path.getmtime) if c else None

    def run_acquisition(exp_id, n_pos, n_t, dt, af, mode='every_timepoint', n=1, sample_live=False):
        mp.clear()
        app.processEvents()
        for i in range(n_pos):
            move_to(start_x + i * spacing, start_y, start_z)
            mp.add_location()
            app.processEvents()
        mp.lineEdit_savingDir.setText(save_dir)
        mpc.set_base_path(save_dir)
        mp.lineEdit_experimentID.setText(exp_id)
        mp.entry_NX.setValue(1); mp.entry_NY.setValue(1); mp.entry_NZ.setValue(1)
        mp.entry_Nt.setValue(n_t); mp.entry_dt.setValue(dt)
        mp.list_configurations.clearSelection()
        for name in (ch0, ch1):
            for item in mp.list_configurations.findItems(name, Qt.MatchExactly):
                item.setSelected(True)
        mp.checkbox_withAutofocus.setChecked(af)
        mp.checkbox_save_multichannel.setChecked(False)
        afc.set_af_schedule(mode=mode, n=n, all_at_t0=True)
        app.processEvents()
        samples = dict(countdown=[], heads=[], label_visible=[], bar_max=0)

        def sample():
            m = mp.live.model
            c = m.countdown_text()
            if c:
                samples['countdown'].append(c)
                samples['label_visible'].append(mp.live.label_count.isVisibleTo(mp.live))
            samples['heads'].append(m.headline()[0])
            samples['bar_max'] = max(samples['bar_max'], mp.live.bar_overall.value())
        loop = QEventLoop()
        mpc.acquisitionFinished.connect(loop.quit)
        QTimer.singleShot(int(args.timeout_s * 1000), loop.quit)
        timer = QTimer(); timer.setInterval(200); timer.timeout.connect(sample); timer.start()
        t0 = time.time()
        mp.btn_startAcquisition.click()
        loop.exec_()
        timer.stop()
        finished = (time.time() - t0) < args.timeout_s - 1
        try:
            mpc.acquisitionFinished.disconnect(loop.quit)
        except TypeError:
            pass
        if not finished:
            mpc.request_abort_aquisition()
            pump(5)
        pump(1)
        return finished, time.time() - t0, samples

    names = args.scenarios.upper()
    n_pos, n_t, n_ch = 3, 3, 2

    # ================================================================== A (+C): autofocus at every time point, countdown
    if 'A' in names or 'C' in names:
        scn = 'A'
        print(f'\n--- A/C: {n_pos} positions, {n_t} time points, dt={args.dt} s, autofocus at every time point')
        try:
            ok, secs, samples = run_acquisition('flexA', n_pos, n_t, args.dt, True, 'every_timepoint')
            record(scn, 'acquisition finished before the timeout', ok, f'{secs:.0f} s')
            exp = find_exp('flexA')
            record(scn, 'experiment folder created', exp is not None)
            rows = read_log(exp) if exp else None
            record(scn, 'position_log.csv written in the experiment folder', rows is not None)
            if rows:
                cols = ['time', 'time_point', 'position', 'id', 'event', 'x_list_mm', 'y_list_mm', 'z_list_um', 'x_stage_mm',
                        'y_stage_mm', 'z_stage_um', 'z_before_af_um', 'z_widget_um', 'z_original_um', 'note']
                record(scn, 'position log has the expected columns', list(rows[0].keys()) == cols, str(list(rows[0].keys())))
                for ev in ('arrive', 'af', 'end'):
                    k = sum(1 for r in rows if r['event'] == ev)
                    record(scn, f'position log: {n_pos * n_t} "{ev}" rows', k == n_pos * n_t, f'{k}')
                af_rows = {(int(r['time_point']), int(r['position'])): r for r in rows if r['event'] == 'af'}
                arr_rows = {(int(r['time_point']), int(r['position'])): r for r in rows if r['event'] == 'arrive'}
                bad_stored, bad_peak, bad_carry, bad_stage, bad_widget = [], [], [], [], []
                for (t, p), r in sorted(af_rows.items()):
                    zl = float(r['z_list_um']); zs = float(r['z_stage_um'])
                    if not r['note'].startswith('focused z stored'):
                        bad_stored.append((t, p, r['note']))
                    if abs(zs - peak_um(p, t)) > 1.5:
                        bad_peak.append((t, p, round(zs, 2), round(peak_um(p, t), 2)))
                    if abs(zl - zs) > 0.6:                     # list = stage z after the autofocus
                        bad_stored.append((t, p, f'list {zl:.2f} stage {zs:.2f}'))
                    if r['z_widget_um'] == '' or abs(float(r['z_widget_um']) - zl) > 0.01:
                        bad_widget.append((t, p, r['z_widget_um'], zl))
                    if t > 0:
                        prev = float(af_rows[(t - 1, p)]['z_list_um'])
                        z_arr = float(arr_rows[(t, p)]['z_list_um'])
                        if abs(z_arr - prev) > 0.01:
                            bad_carry.append((t, p, z_arr, prev))
                        if abs(float(arr_rows[(t, p)]['z_stage_um']) - z_arr) > 1.0:
                            bad_stage.append((t, p, arr_rows[(t, p)]['z_stage_um'], z_arr))
                record(scn, 'autofocus finds the known focus (z of the stage after AF)', not bad_peak, str(bad_peak[:4]))
                record(scn, 'focused z is stored in the positions list', not bad_stored, str(bad_stored[:4]))
                record(scn, 'positions list (widget) holds the same z as the acquisition', not bad_widget, str(bad_widget[:4]))
                record(scn, 'next time point starts from the z found at the previous time point (list)', not bad_carry, str(bad_carry[:4]))
                record(scn, 'the stage is at that z when the next time point arrives', not bad_stage, str(bad_stage[:4]))
                # the displayed table
                tab_bad = []
                for p in range(n_pos):
                    z_um = mp.location_list[p, 2] * 1000
                    cell = mp.table_location_list.item(p, 2).text()
                    if abs(float(cell) - z_um) > 0.06:
                        tab_bad.append((p, cell, round(z_um, 2)))
                record(scn, 'Positions table shows the focused z (not the old one)', not tab_bad, str(tab_bad))
                last_z = {p: float(af_rows[(n_t - 1, p)]['z_list_um']) for p in range(n_pos)}
                record(scn, 'positions list ends with the z of the last autofocus',
                       all(abs(mp.location_list[p, 2] * 1000 - last_z[p]) < 0.01 for p in range(n_pos)))
                record(scn, 'the z of the positions really changed (test is not trivial)',
                       any(abs(last_z[p] - start_z * 1000) > 1.0 for p in range(n_pos)), str(last_z))
            # --- the countdown / progress view (scenario C)
            model = mp.live.model
            record('C', 'live view: countdown "Next acquisition in ..." shown while waiting', len(samples['countdown']) > 0, str(samples['countdown'][:2]))
            record('C', 'live view: countdown label is visible', len(samples['label_visible']) > 0 and all(samples['label_visible']))
            if samples['countdown']:
                import re
                secs_left = [int(re.search(r'in (\d+) s', c).group(1)) for c in samples['countdown']]
                record('C', 'live view: countdown counts down', any(a > b for a, b in zip(secs_left, secs_left[1:])) and sum(1 for a, b in zip(secs_left, secs_left[1:]) if b > a) <= n_t - 1, str(secs_left[:6]))
            record('C', 'live view: all images counted', model.images_done == n_pos * n_t * n_ch, f'{model.images_done} of {n_pos * n_t * n_ch}')
            record('C', 'live view: progress bar reached the planned number of images',
                   mp.live.bar_overall.value() == n_pos * n_t * n_ch and mp.live.bar_overall.maximum() == n_pos * n_t * n_ch,
                   f'{mp.live.bar_overall.value()} / {mp.live.bar_overall.maximum()}')
            record('C', 'live view: status "completed"', model.status == 'completed', str(model.status))
            record('C', 'live view: headline says Acquisition completed', 'Acquisition completed' in model.headline()[0], model.headline()[0])
            record('C', 'live view: all time points done', all(s == 'done' for _, s in model.tp_boxes()) and len(model.tp_boxes()) == n_t, str(model.tp_boxes()))
            record('C', 'live view: positions of the last time point all done',
                   len(model.position_boxes()) == n_pos and all(s == 'done' for _, s in model.position_boxes()), str(model.position_boxes()))
            record('C', 'live view is visible in the tab after the start', mp.live.isVisibleTo(mp))
            record('C', 'time points started on time (none late)', not any(v > 3 for v in model.tp_late.values()), str(model.tp_late))
        except Exception:
            record(scn, 'scenario ran without exception', False, traceback.format_exc().splitlines()[-1])
            traceback.print_exc()

    # ================================================================== B: skipped autofocus
    if 'B' in names:
        scn = 'B'
        print(f'\n--- B: {n_pos} positions, {n_t} time points, autofocus every 2nd time point (t=0 and t=2), continuous')
        try:
            ok, secs, samples = run_acquisition('flexB', n_pos, n_t, 0, True, 'every_n', 2)
            record(scn, 'acquisition finished before the timeout', ok, f'{secs:.0f} s')
            rows = read_log(find_exp('flexB')) or []
            skipped = {(int(r['time_point']), int(r['position'])) for r in rows if r['event'] == 'af_skipped'}
            record(scn, 'af_skipped rows for every position of time point 1 and no others', skipped == {(1, p) for p in range(n_pos)}, str(sorted(skipped)))
            af_rows = {(int(r['time_point']), int(r['position'])): r for r in rows if r['event'] == 'af'}
            arr = {(int(r['time_point']), int(r['position'])): r for r in rows if r['event'] == 'arrive'}
            record(scn, 'autofocus rows at time points 0 and 2 only', set(af_rows) == {(t, p) for t in (0, 2) for p in range(n_pos)}, str(sorted(af_rows)))
            bad = [(p, arr[(1, p)]['z_list_um'], af_rows[(0, p)]['z_list_um']) for p in range(n_pos)
                   if abs(float(arr[(1, p)]['z_list_um']) - float(af_rows[(0, p)]['z_list_um'])) > 0.01]
            bad += [(p, arr[(2, p)]['z_list_um'], af_rows[(0, p)]['z_list_um']) for p in range(n_pos)
                    if abs(float(arr[(2, p)]['z_list_um']) - float(af_rows[(0, p)]['z_list_um'])) > 0.01]
            record(scn, 'z stays at the last focus while the autofocus is skipped', not bad, str(bad[:3]))
            record(scn, 'continuous acquisition: no countdown shown', len(samples['countdown']) == 0, str(samples['countdown'][:2]))
        except Exception:
            record(scn, 'scenario ran without exception', False, traceback.format_exc().splitlines()[-1])
            traceback.print_exc()

    def wait_autofocus(timeout=60):
        t0 = time.time()
        while time.time() - t0 < timeout:
            app.processEvents()
            time.sleep(0.02)
            if not afc.autofocus_in_progress:
                break
        pump(0.5)
        return not afc.autofocus_in_progress

    # ================================================================== D: single autofocus uses the AF channel
    if 'D' in names:
        scn = 'D'
        print('\n--- D: single autofocus test')
        try:
            move_to(start_x, start_y, start_z)
            afc.set_af_channel(ch1)
            live.dropdown_modeSelection.setCurrentText(ch0)       # the live channel is NOT the autofocus channel
            pump(0.3)
            record(scn, 'live channel set for the test', liveC.currentConfiguration.name == ch0, str(liveC.currentConfiguration.name))
            afc.last_error = None
            # the old behaviour (for comparison): autofocus(False) uses the live channel
            del measure_log[:]
            afc.autofocus(False)
            record(scn, 'plain autofocus() finished', wait_autofocus())
            used_old = {n for n, _ in measure_log}
            record(scn, 'plain autofocus() still uses the live channel (unchanged)', used_old == {ch0}, str(used_old))
            # the new single test
            del measure_log[:]
            record(scn, 'AutoFocusController.autofocus_single exists', hasattr(afc, 'autofocus_single'))
            if hasattr(afc, 'autofocus_single'):
                afc.autofocus_single()
                record(scn, 'single autofocus finished', wait_autofocus())
                used = {n for n, _ in measure_log}
                record(scn, 'single autofocus takes its images with the AF channel', used == {ch1}, f'used {used}, AF channel {ch1}')
                record(scn, 'live channel is put back afterwards (controller)', liveC.currentConfiguration.name == ch0, str(liveC.currentConfiguration.name))
                record(scn, 'live channel is put back afterwards (dropdown)', live.dropdown_modeSelection.currentText() == ch0, live.dropdown_modeSelection.currentText())
                record(scn, 'single autofocus without error', not getattr(afc, 'last_error', None), str(getattr(afc, 'last_error', None)))
            # the buttons are connected to the new function
            btn = win.autofocusWidget.btn_autofocus
            del measure_log[:]
            btn.click()
            record(scn, 'Autofocus button (Contrast AF tab) finished', wait_autofocus())
            used = {n for n, _ in measure_log}
            record(scn, 'Autofocus button uses the AF channel', used == {ch1}, f'used {used}')
            record(scn, 'live channel is put back after the button', liveC.currentConfiguration.name == ch0)
        except Exception:
            record(scn, 'scenario ran without exception', False, traceback.format_exc().splitlines()[-1])
            traceback.print_exc()

    # ================================================================== E: autofocus with hardware trigger
    if 'E' in names:
        scn = 'E'
        print('\n--- E: autofocus in hardware-trigger mode')
        mc = nav.microcontroller
        orig_hw = mc.send_hardware_trigger
        calls = []

        def fake_hw_trigger(control_illumination=False, illumination_on_time_us=0, trigger_output_ch=0):
            calls.append((control_illumination, illumination_on_time_us))
            win.camera.send_trigger()           # what the camera does on the hardware trigger
        mc.send_hardware_trigger = fake_hw_trigger
        try:
            liveC.set_trigger_mode(TriggerMode.HARDWARE)
            record(scn, 'trigger mode is HARDWARE', liveC.trigger_mode == TriggerMode.HARDWARE)
            move_to(start_x, start_y, start_z)
            live.dropdown_modeSelection.setCurrentText(ch1)
            pump(0.3)
            afc.last_error = None
            del measure_log[:]
            afc.autofocus(False)
            record(scn, 'autofocus finished', wait_autofocus())
            record(scn, 'autofocus without error in hardware-trigger mode', not getattr(afc, 'last_error', None), str(getattr(afc, 'last_error', None)))
            record(scn, 'hardware trigger sent with illumination control for the autofocus images',
                   len(calls) >= 2 and all(c[0] for c in calls), f'{len(calls)} triggers')
            record(scn, 'every autofocus image was evaluated', len(measure_log) == len(calls), f'{len(measure_log)} images, {len(calls)} triggers')
        except Exception:
            record(scn, 'scenario ran without exception', False, traceback.format_exc().splitlines()[-1])
            traceback.print_exc()
        finally:
            mc.send_hardware_trigger = orig_hw
            try:
                liveC.set_trigger_mode(TriggerMode.SOFTWARE)
            except Exception:
                pass

    afc.compute_focus_measure = orig_measure
    n_fail = sum(1 for r in results if not r[2])
    report = os.path.join(save_dir, f'flexible_e2e_report_{time.strftime("%Y%m%d_%H%M%S")}.txt')
    with open(report, 'w') as f:
        for scn, name, ok, detail in results:
            f.write(f'{"PASS" if ok else "FAIL"}  {scn}: {name}  {detail if not ok else ""}\n')
    print('\n' + '=' * 70)
    print(f'{len(results) - n_fail} passed, {n_fail} failed. Report: {report}')
    sys.stdout.flush()
    os._exit(1 if n_fail else 0)


if __name__ == '__main__':
    main()
