"""End-to-end acquisition test for the Flexible Multipoint workflow (simulation or real microscope).

It starts the real GUI, defines positions, runs real acquisitions with the real MultiPointWorker and
afterwards checks (1) which positions got autofocus at which time point against the schedule,
(2) that the expected image files exist, (3) the saved parameters.

Run from anywhere (the script changes into the software folder itself):

  Simulation (no hardware, safe):
    python tests/e2e_acquisition.py --simulation --save-dir C:\\temp\\squid_e2e

  Real microscope (MOVES THE STAGE, runs real autofocus):
    python tests/e2e_acquisition.py --hardware --save-dir D:\\test_runs --n-positions 3 --spacing-mm 0.5

Starting position: --start-x/--start-y/--start-z (mm). Without them the current stage position is used.
Positions lie on a line in +x: start + i*spacing. At the end the stage returns to the start position.
Choose which scenarios run with --scenarios (default: all), see SCENARIOS below.
"""
import os
import sys
import json
import time
import argparse
import glob
import traceback

SOFTWARE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---- test scenarios (independent of the code under test) ------------------------------------------
# name: dict(n_positions, n_timepoints, af (checkbox), mode, n, all_t0, multichannel)
SCENARIOS = {
    'af_every_timepoint': dict(n_t=2, af=True,  mode='every_timepoint', n=1, all_t0=True,  multichannel=False),
    'af_every_n':         dict(n_t=5, af=True,  mode='every_n',         n=2, all_t0=True,  multichannel=False),
    'af_staggered':       dict(n_t=4, af=True,  mode='staggered',       n=3, all_t0=True,  multichannel=False, min_pos=6),
    'af_staggered_no_t0': dict(n_t=4, af=True,  mode='staggered',       n=3, all_t0=False, multichannel=False, min_pos=6),
    'af_off':             dict(n_t=2, af=False, mode='every_timepoint', n=1, all_t0=True,  multichannel=False),
    'multichannel':       dict(n_t=2, af=False, mode='every_timepoint', n=1, all_t0=True,  multichannel=True),
}


def expected_af(mode, n, all_t0, n_t, n_pos, af_on=True):
    """Set of (time_point, position) that must get an autofocus. Both counted from 0.
    Written from the specification, not copied from the controller."""
    if not af_on:
        return set()
    out = set()
    for t in range(n_t):
        for p in range(n_pos):
            if mode == 'every_timepoint':
                ok = True
            elif mode == 'every_n':
                ok = (t % n == 0)
            elif mode == 'staggered':
                ok = (all_t0 and t == 0) or ((p - t) % n == 0)
            else:
                raise ValueError(mode)
            if ok:
                out.add((t, p))
    return out


def find_experiment_dir(save_dir, experiment_id):
    cands = [d for d in glob.glob(os.path.join(save_dir, experiment_id + '_*')) if os.path.isdir(d)]
    return max(cands, key=os.path.getmtime) if cands else None


def check_files(exp_dir, n_t, n_pos, n_channels, multichannel):
    """Return list of (name, ok, detail)."""
    res = []
    res.append(('acquisition parameters.json exists', os.path.isfile(os.path.join(exp_dir, 'acquisition parameters.json')), ''))
    try:
        params = json.load(open(os.path.join(exp_dir, 'acquisition parameters.json')))
        res.append(('parameters: Nt matches', params.get('Nt') == n_t, f"Nt={params.get('Nt')}"))
    except Exception as e:
        res.append(('parameters readable', False, str(e)))
    res.append(('configurations.xml exists', os.path.isfile(os.path.join(exp_dir, 'configurations.xml')), ''))
    for t in range(n_t):
        folder = os.path.join(exp_dir, str(t))
        imgs = [f for f in glob.glob(os.path.join(folder, '*')) if f.lower().endswith(('.tiff', '.tif', '.bmp', '.png', '.jpg'))
                and 'focus_camera' not in f and 'laser af' not in f]
        if multichannel:
            # per-position multichannel tiffs live in the experiment folder
            mc = glob.glob(os.path.join(exp_dir, f'Time{t:04d}_Point*.tiff'))
            res.append((f'time point {t}: {n_pos} multichannel files', len(mc) == n_pos, f'found {len(mc)}'))
        else:
            res.append((f'time point {t}: {n_pos * n_channels} images', len(imgs) == n_pos * n_channels, f'found {len(imgs)}'))
    if multichannel:
        try:
            import tifffile
            mc = sorted(glob.glob(os.path.join(exp_dir, 'Time0000_Point0000.tiff')))
            if mc:
                shape = tifffile.imread(mc[0]).shape
                res.append(('multichannel file has one plane per channel', n_channels in shape, f'shape {shape}'))
        except Exception as e:
            res.append(('multichannel file readable', False, str(e)))
    return res


class Recorder:
    """Wraps the controller's AF gate and AF call to log what the acquisition does."""
    def __init__(self, af_controller):
        self.c = af_controller
        self.gate = []       # (t, p, result)
        self.af_calls = 0
        self._orig_gate = af_controller.autofocus_scheduled
        self._orig_af = af_controller.autofocus

        def gate(t, p):
            r = self._orig_gate(t, p)
            self.gate.append((t, p, bool(r)))
            return r

        def af(*a, **k):
            self.af_calls += 1
            return self._orig_af(*a, **k)

        af_controller.autofocus_scheduled = gate
        af_controller.autofocus = af

    def restore(self):
        self.c.autofocus_scheduled = self._orig_gate
        self.c.autofocus = self._orig_af


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument('--simulation', action='store_true', help='simulated hardware (safe)')
    mode.add_argument('--hardware', action='store_true', help='REAL microscope: moves the stage')
    ap.add_argument('--save-dir', required=True, help='existing base saving directory for the test experiments')
    ap.add_argument('--start-x', type=float, help='mm; default: current stage position')
    ap.add_argument('--start-y', type=float, help='mm; default: current stage position')
    ap.add_argument('--start-z', type=float, help='mm; default: current stage position')
    ap.add_argument('--n-positions', type=int, default=None, help='default 3 (6 for the staggered scenarios)')
    ap.add_argument('--spacing-mm', type=float, default=0.5, help='distance between positions along +x')
    ap.add_argument('--n-channels', type=int, default=2)
    ap.add_argument('--dt', type=float, default=0.0, help='time between time points (s)')
    ap.add_argument('--scenarios', default='all', help='comma separated names or "all": ' + ', '.join(SCENARIOS))
    ap.add_argument('--timeout-s', type=float, default=900, help='per scenario')
    ap.add_argument('--yes', action='store_true', help='skip the confirmation question for --hardware')
    ap.add_argument('--max-span-mm', type=float, default=20.0, help='safety limit for hardware runs')
    args = ap.parse_args()

    if not os.path.isdir(args.save_dir):
        sys.exit(f'Saving directory does not exist: {args.save_dir}')
    names = list(SCENARIOS) if args.scenarios == 'all' else [s.strip() for s in args.scenarios.split(',')]
    for n in names:
        if n not in SCENARIOS:
            sys.exit(f'Unknown scenario {n}. Available: {", ".join(SCENARIOS)}')

    os.chdir(SOFTWARE_DIR)
    sys.path.insert(0, SOFTWARE_DIR)
    os.environ['QT_API'] = 'pyqt5'
    from qtpy.QtCore import QEventLoop, QTimer, Qt
    from qtpy.QtWidgets import QApplication
    import control.gui_hcs as gui

    app = QApplication([])
    app.setStyle('Fusion')
    win = gui.OctopiGUI(is_simulation=args.simulation)
    win.show()
    app.processEvents()

    nav = win.navigationController
    mp = win.multiPointWidget2
    mpc = win.multipointController
    afc = win.autofocusController

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

    refresh_position()
    start = (args.start_x if args.start_x is not None else nav.x_pos_mm,
             args.start_y if args.start_y is not None else nav.y_pos_mm,
             args.start_z if args.start_z is not None else nav.z_pos_mm)
    channel_names = [c.name for c in win.configurationManager.configurations][:args.n_channels]

    print('=' * 70)
    print('mode          :', 'REAL HARDWARE' if args.hardware else 'simulation')
    print('saving dir    :', args.save_dir)
    print('start x,y,z mm:', start)
    print('channels      :', channel_names)
    print('scenarios     :', names)
    print('=' * 70)
    if args.hardware:
        n_max = max(args.n_positions or 3, 6)
        span = args.spacing_mm * (n_max - 1)
        if span > args.max_span_mm:
            sys.exit(f'Refusing: span {span} mm exceeds --max-span-mm {args.max_span_mm}')
        print(f'The stage will move from x={start[0]} to x={start[0] + span} mm (y={start[1]}, z={start[2]}) and autofocus will run.')
        if not args.yes and input('Type MOVE to continue: ').strip() != 'MOVE':
            sys.exit('Aborted.')

    all_results = []  # (scenario, check, ok, detail)

    def record(scn, name, ok, detail=''):
        all_results.append((scn, name, bool(ok), detail))
        print(('  PASS  ' if ok else '  FAIL  ') + name + (f'   [{detail}]' if detail and not ok else ''))

    for scn in names:
        cfg = SCENARIOS[scn]
        n_pos = args.n_positions or cfg.get('min_pos', 3)
        n_pos = max(n_pos, cfg.get('min_pos', 1)) if scn.startswith('af_staggered') else n_pos
        print(f'\n--- scenario {scn}: {n_pos} positions, {cfg["n_t"]} time points, AF={cfg["af"]} '
              f'{cfg["mode"]} n={cfg["n"]} all_t0={cfg["all_t0"]} multichannel={cfg["multichannel"]}')
        rec = None
        try:
            # --- positions
            mp.clear()
            app.processEvents()
            for i in range(n_pos):
                move_to(start[0] + i * args.spacing_mm, start[1], start[2])
                mp.add_location()
                app.processEvents()
            record(scn, 'positions defined', len(mp.location_list) == n_pos, f'{len(mp.location_list)}')

            # --- settings
            mp.lineEdit_savingDir.setText(args.save_dir)
            mpc.set_base_path(args.save_dir)
            mp.lineEdit_experimentID.setText(scn)
            mp.entry_NX.setValue(1); mp.entry_NY.setValue(1); mp.entry_NZ.setValue(1)
            mp.entry_Nt.setValue(cfg['n_t']); mp.entry_dt.setValue(args.dt)
            mp.list_configurations.clearSelection()
            for name in channel_names:
                for item in mp.list_configurations.findItems(name, Qt.MatchExactly):
                    item.setSelected(True)
            mp.checkbox_withAutofocus.setChecked(cfg['af'])
            mp.checkbox_save_multichannel.setChecked(cfg['multichannel'])
            afc.set_af_schedule(mode=cfg['mode'], n=cfg['n'], all_at_t0=cfg['all_t0'])
            app.processEvents()

            # --- run
            rec = Recorder(afc)
            loop = QEventLoop()
            mpc.acquisitionFinished.connect(loop.quit)
            QTimer.singleShot(int(args.timeout_s * 1000), loop.quit)
            t0 = time.time()
            mp.btn_startAcquisition.click()
            loop.exec_()
            finished = (time.time() - t0) < args.timeout_s - 1
            try:
                mpc.acquisitionFinished.disconnect(loop.quit)
            except TypeError:
                pass
            record(scn, 'acquisition finished before timeout', finished, f'{time.time() - t0:.0f} s')
            if not finished:
                mpc.request_abort_aquisition()
                pump(5)
            pump(1)

            # --- AF schedule
            exp = expected_af(cfg['mode'], cfg['n'], cfg['all_t0'], cfg['n_t'], n_pos, cfg['af'])
            gated_true = {(t, p) for t, p, r in rec.gate if r}
            gated_all = {(t, p) for t, p, r in rec.gate}
            if cfg['af']:
                record(scn, 'AF gate evaluated for every (time point, position)',
                       gated_all == {(t, p) for t in range(cfg['n_t']) for p in range(n_pos)},
                       f'evaluated {len(gated_all)} of {cfg["n_t"] * n_pos}')
            else:
                record(scn, 'AF gate not evaluated when AF is off', len(rec.gate) == 0, f'{len(rec.gate)}')
            record(scn, 'AF at exactly the expected (t, position) pairs', gated_true == exp,
                   f'missing {sorted(exp - gated_true)}, unexpected {sorted(gated_true - exp)}')
            record(scn, 'number of autofocus runs equals expectation', rec.af_calls == len(exp),
                   f'ran {rec.af_calls}, expected {len(exp)}')

            # --- files
            exp_dir = find_experiment_dir(args.save_dir, scn)
            record(scn, 'experiment folder created', exp_dir is not None)
            if exp_dir:
                for name, ok, detail in check_files(exp_dir, cfg['n_t'], n_pos, len(channel_names), cfg['multichannel']):
                    record(scn, name, ok, detail)
        except Exception:
            record(scn, 'scenario ran without exception', False, traceback.format_exc().splitlines()[-1])
            traceback.print_exc()
        finally:
            if rec is not None:
                rec.restore()
            try:
                if mp.btn_startAcquisition.isChecked():
                    mpc.request_abort_aquisition()
                    mp.btn_startAcquisition.setChecked(False)
                    mp.setEnabled_all(True)
            except Exception:
                pass

    move_to(*start)

    n_fail = sum(1 for r in all_results if not r[2])
    report = os.path.join(args.save_dir, f'e2e_report_{time.strftime("%Y%m%d_%H%M%S")}.txt')
    with open(report, 'w') as f:
        f.write(f'mode: {"hardware" if args.hardware else "simulation"}\nstart: {start}\n\n')
        for scn, name, ok, detail in all_results:
            f.write(f'{"PASS" if ok else "FAIL"}  {scn}: {name}  {detail if not ok else ""}\n')
    print('\n' + '=' * 70)
    print(f'{len(all_results) - n_fail} passed, {n_fail} failed. Report: {report}')
    win.close()
    sys.exit(1 if n_fail else 0)


if __name__ == '__main__':
    main()
