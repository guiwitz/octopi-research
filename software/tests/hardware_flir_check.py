"""Check the FLIR camera and the FLIR driver (control/camera_flir.py) on the microscope computer.

Only the camera is used: the stage does not move and no illumination is switched on (images are dark; the check
uses the exposure the camera reports for each frame - "chunk data" - not the image brightness).
Close the Squid software first (the camera can only be opened by one program).

Run in the software folder of the branch, with its pixi environment:
    pixi run python tests/hardware_flir_check.py                 # about 3 minutes
    pixi run python tests/hardware_flir_check.py --quick         # about 30 s
    python tests/hardware_flir_check.py --fake                   # without camera (fake PySpin, to test this script)

Writes flir_check_<date>.txt and flir_check_<date>_frames.csv next to this file; send both.

Checks:
  1. open, MONO16, software trigger, exposure and gain are applied (nodes read back)
  2. exposures 10 ms ... 2 s: every frame arrives (no timeout) and the camera reports the exposure that was set
  3. channel switching (different exposures one after the other, as in a multipoint acquisition): does each frame
     carry the exposure set for it, or the one of the previous channel? (the 'exposure of channel n-1' bug)
  4. live <-> acquisition switches (callback on/off) many times: no error, no stale frame afterwards
  5. no trigger: read_frame returns None after its timeout, and raises after 3 failures in a row
  6. rolling-shutter readout measured on the camera (8 and 16 bit) and the resulting hardware-trigger timing
     (strobe delay, camera exposure); no trigger pulses are sent
  7. exposure AND gain switching by software, frame by frame (12 ms/gain 0, 22 ms/gain 10, 100 ms/gain 20): does each
     frame carry its own gain? how long does writing the gain take? (today's way of switching channels)
  8. Sequencer (for multi-channel hardware triggering, nothing implemented yet): which Sequencer nodes the camera has;
     then 3 sets with their own exposure and gain, stepped through with software triggers: does every frame get the
     exposure and gain of its set, in order? Also: switching the trigger source to Line2 with the Sequencer on (no
     pulses), and the time to change one set. The Sequencer is switched off at the end and a normal frame is checked.
     A camera without Sequencer is reported (INFO), not counted as a failure.
"""
import os
import sys
import csv
import time
import argparse
import datetime
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
SW = os.path.dirname(HERE)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fake', action='store_true', help='use the fake PySpin (no camera needed)')
    ap.add_argument('--fake-no-sequencer', action='store_true', help='with --fake: a camera without Sequencer')
    ap.add_argument('--quick', action='store_true')
    ap.add_argument('--pixel-format', default='MONO16')
    ap.add_argument('--out', default=HERE)
    a = ap.parse_args()
    os.chdir(SW)
    sys.path.insert(0, SW)
    if a.fake:
        sys.path.insert(0, HERE)
        import fake_pyspin
        sys.modules['PySpin'] = fake_pyspin
        fake_pyspin.reset(fake_pyspin.FakeCamera(sequencer=not a.fake_no_sequencer))
    import PySpin
    import control.camera_flir as cf

    stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    report_path = os.path.join(a.out, f'flir_check_{stamp}.txt')
    frames_path = os.path.join(a.out, f'flir_check_{stamp}_frames.csv')
    lines, results, frames = [], [], []

    def say(text):
        print(text, flush=True)
        lines.append(text)

    def check(name, ok, detail=''):
        results.append((name, bool(ok)))
        say(('PASS  ' if ok else 'FAIL  ') + name + (f'   [{detail}]' if detail else ''))

    # --- capture the camera's own frame information (chunk data) behind camera_flir's back
    last = {}

    def enable_chunks(cam):
        nm = cam.camera.GetNodeMap()
        try:
            PySpin.CBooleanPtr(nm.GetNode('ChunkModeActive')).SetValue(True)
            sel = PySpin.CEnumerationPtr(nm.GetNode('ChunkSelector'))
            for name in ('ExposureTime', 'FrameID', 'Timestamp', 'Gain', 'SequencerSetActive'):
                e = sel.GetEntryByName(name)
                if e is not None and PySpin.IsReadable(e):
                    sel.SetIntValue(e.GetValue())
                    PySpin.CBooleanPtr(nm.GetNode('ChunkEnable')).SetValue(True)
            return True
        except Exception as ex:
            say('chunk data not available: ' + str(ex))
            return False

    class CameraProxy:
        """Stands in for the PySpin camera object inside camera_flir: everything is passed through, GetNextImage also
        records the camera's own frame information (works even if the SWIG object does not accept new attributes)."""
        def __init__(self, real):
            self._real = real

        def __getattr__(self, name):
            return getattr(self._real, name)

        def GetNextImage(self, *args):
            last.clear()
            t0 = time.time()
            img = self._real.GetNextImage(*args)
            last['wait_s'] = time.time() - t0
            try:
                ch = img.GetChunkData()
                last['chunk_exposure_ms'] = ch.GetExposureTime() / 1000.0
                last['chunk_frame_id'] = ch.GetFrameID()
            except Exception:
                pass
            try:
                last['chunk_gain'] = float(img.GetChunkData().GetGain())
            except Exception:
                pass
            try:
                last['chunk_sequencer_set'] = int(img.GetChunkData().GetSequencerSetActive())
            except Exception:
                try:   # older PySpin: the chunk value is also in the node map after GetNextImage
                    nd = self._real.GetNodeMap().GetNode('ChunkSequencerSetActive')
                    if nd is not None and PySpin.IsReadable(nd):
                        last['chunk_sequencer_set'] = int(PySpin.CIntegerPtr(nd).GetValue())
                except Exception:
                    pass
            return img

    def wrap_get_next_image(cam):
        cam.camera = CameraProxy(cam.camera)

    def snap(cam, exposure_ms, label, gain=None, configure=True, expected_set=None):
        """configure=False: do not write exposure/gain (Sequencer: the set decides); they are only the expected values"""
        gain_write_s = None
        if configure:
            cam.set_exposure_time(exposure_ms)
            if gain is not None:
                t_g = time.time()
                cam.set_analog_gain(gain)
                gain_write_s = round(time.time() - t_g, 4)
        t0 = time.time()
        cam.send_trigger()
        img = cam.read_frame()
        dt = time.time() - t0
        row = dict(test=label, set_exposure_ms=exposure_ms, got_image=img is not None, total_s=round(dt, 3),
                   camera_exposure_ms=last.get('chunk_exposure_ms'), camera_frame_id=last.get('chunk_frame_id'),
                   dtype=getattr(img, 'dtype', None), shape=getattr(img, 'shape', None),
                   mean=float(img.mean()) if img is not None else None,
                   set_gain=gain, camera_gain=last.get('chunk_gain'), gain_write_s=gain_write_s,
                   expected_set=expected_set, camera_sequencer_set=last.get('chunk_sequencer_set'))
        frames.append(row)
        return img, row

    def close_enough(row, tol=0.05):
        e = row['camera_exposure_ms']
        return e is None or abs(e - row['set_exposure_ms']) <= max(0.05, tol * row['set_exposure_ms'])

    def gain_ok(row, tol=0.2):
        g = row.get('camera_gain')
        return g is None or row.get('set_gain') is None or abs(g - row['set_gain']) <= tol

    # --- generic node access by name (works for enumeration / integer / float / boolean / command nodes)
    def gnode(name):
        try:
            nd = cam.camera.GetNodeMap().GetNode(name)
        except Exception:
            return None
        return nd if (nd is not None and PySpin.IsAvailable(nd)) else None

    def gget(name):
        nd = gnode(name)
        if nd is None or not PySpin.IsReadable(nd):
            return None
        t = nd.GetPrincipalInterfaceType()
        if t == PySpin.intfIEnumeration:
            return PySpin.CEnumerationPtr(nd).GetCurrentEntry().GetSymbolic()
        if t == PySpin.intfIInteger:
            return PySpin.CIntegerPtr(nd).GetValue()
        if t == PySpin.intfIFloat:
            return PySpin.CFloatPtr(nd).GetValue()
        if t == PySpin.intfIBoolean:
            return PySpin.CBooleanPtr(nd).GetValue()
        return PySpin.CValuePtr(nd).ToString()

    def gset(name, value=None):
        nd = gnode(name)
        if nd is None:
            raise RuntimeError(f'node {name} not available')
        if not PySpin.IsWritable(nd):
            raise RuntimeError(f'node {name} not writable')
        t = nd.GetPrincipalInterfaceType()
        if t == PySpin.intfICommand:
            PySpin.CCommandPtr(nd).Execute()
        elif t == PySpin.intfIEnumeration:
            en = PySpin.CEnumerationPtr(nd)
            entry = en.GetEntryByName(str(value))
            if entry is None or not PySpin.IsReadable(entry):
                raise RuntimeError(f'{name}: no entry {value}')
            en.SetIntValue(entry.GetValue())
        elif t == PySpin.intfIInteger:
            PySpin.CIntegerPtr(nd).SetValue(int(value))
        elif t == PySpin.intfIFloat:
            PySpin.CFloatPtr(nd).SetValue(float(value))
        elif t == PySpin.intfIBoolean:
            PySpin.CBooleanPtr(nd).SetValue(bool(value))
        else:
            raise RuntimeError(f'{name}: unsupported node type {t}')

    def describe(name):
        nd = gnode(name)
        if nd is None:
            return 'missing'
        acc = ('R' if PySpin.IsReadable(nd) else '-') + ('W' if PySpin.IsWritable(nd) else '-')
        try:
            txt = f'{acc} value={gget(name)}'
        except Exception:
            txt = f'{acc}'
        try:
            t = nd.GetPrincipalInterfaceType()
            if t == PySpin.intfIEnumeration:
                ents = [PySpin.CEnumEntryPtr(e).GetSymbolic() for e in PySpin.CEnumerationPtr(nd).GetEntries()
                        if PySpin.IsReadable(e)]
                txt += ' entries=' + '/'.join(ents)
            elif t in (PySpin.intfIInteger, PySpin.intfIFloat):
                ptr = PySpin.CIntegerPtr(nd) if t == PySpin.intfIInteger else PySpin.CFloatPtr(nd)
                txt += f' range={ptr.GetMin()}..{ptr.GetMax()}'
        except Exception:
            pass
        return txt

    say(f'FLIR check {stamp}  ({"FAKE PySpin" if a.fake else "real camera"})')
    try:
        say('Spinnaker version: ' + '.'.join(str(getattr(PySpin.System.GetInstance().GetLibraryVersion(), k, '?')) for k in ('major', 'minor', 'type', 'build')))
    except Exception:
        pass

    # 1. open and settings
    cam = cf.Camera(sn=None)
    cam.open()
    try:
        info = cf.get_device_info(cam.camera)
        say('camera: ' + str(info.get('TLDevice', {}).get('DeviceInformation', {}).get('DeviceModelName', '?')))
    except Exception:
        pass
    cam.set_pixel_format(a.pixel_format)
    cam.set_software_triggered_acquisition()
    have_chunks = enable_chunks(cam)
    cam.start_streaming()
    wrap_get_next_image(cam)
    cam.set_exposure_time(20)
    cam.set_analog_gain(10)
    nm = cam.camera.GetNodeMap()
    exp_node = PySpin.CFloatPtr(nm.GetNode('ExposureTime')).GetValue()
    gain_node = PySpin.CFloatPtr(nm.GetNode('Gain')).GetValue()
    check('exposure 20 ms is set in the camera', abs(exp_node - 20000) < 50, f'{exp_node:.0f} us')
    check('gain 10 is set in the camera', abs(gain_node - 10) < 0.2, f'{gain_node:.2f}')
    img, row = snap(cam, 20, 'basic')
    check('software trigger gives an image', img is not None, f'{row["dtype"]} {row["shape"]}')
    if img is not None and a.pixel_format == 'MONO16':
        check('MONO16 image is 16 bit', str(img.dtype) == 'uint16', str(img.dtype))
    check('the camera reports frame information (chunk data)', have_chunks and row['camera_exposure_ms'] is not None,
          'without it checks 2-4 can only test that frames arrive')

    # 2. exposure range
    exposures = [10, 100, 1000, 1500] if a.quick else [10, 50, 100, 300, 900, 1000, 1200, 1500, 2000]
    for e in exposures:
        img, row = snap(cam, e, 'exposure range')
        check(f'exposure {e} ms: frame arrives', img is not None, f'{row["total_s"]} s')
        if img is not None:
            check(f'exposure {e} ms: camera reports {e} ms', close_enough(row), f'camera {row["camera_exposure_ms"]} ms')

    # 3. channel switching
    plan = [12, 22, 100] * (3 if a.quick else 10)
    wrong, missing, prev_match = 0, 0, 0
    for i, e in enumerate(plan):
        img, row = snap(cam, e, 'channel switching')
        if img is None:
            missing += 1
        elif not close_enough(row):
            wrong += 1
            if i > 0 and row['camera_exposure_ms'] is not None and abs(row['camera_exposure_ms'] - plan[i - 1]) < 0.5:
                prev_match += 1
    check(f'channel switching ({len(plan)} frames, 12/22/100 ms): every frame arrives', missing == 0, f'{missing} missing')
    check('channel switching: every frame has the exposure set for it', wrong == 0,
          f'{wrong} wrong, {prev_match} of them with the exposure of the previous channel' if wrong else '')

    # 4. live <-> acquisition
    live_frames = []
    cam.set_callback(lambda c: live_frames.append(1))
    errors, stale = 0, 0
    for i in range(10 if a.quick else 40):
        try:
            cam.set_exposure_time(5)
            cam.enable_callback()
            cam.send_trigger()
            time.sleep(0.05)
            cam.send_trigger()                 # a live frame still in flight when live stops
            cam.disable_callback()
            img, row = snap(cam, 33, 'live switch')
            if img is None or not close_enough(row):
                stale += 1
        except Exception as ex:
            errors += 1
            say('  error in live switch: ' + repr(ex))
    check('live <-> acquisition switches without error', errors == 0, f'{errors} errors')
    check('after live, the acquisition gets its own frame (no stale live frame)', stale == 0, f'{stale} stale or missing')
    check('live callbacks delivered frames', len(live_frames) > 0, f'{len(live_frames)} frames')

    # 5. no trigger -> timeout
    cam.set_exposure_time(10)
    t0 = time.time()
    r = cam.read_frame()                       # no trigger sent
    check('without trigger read_frame returns None (no crash)', r is None, f'after {time.time() - t0:.2f} s')
    raised = False
    try:
        cam.read_frame()
        cam.read_frame()
    except RuntimeError:
        raised = True
    check('3 failed reads in a row raise an error', raised)
    img, row = snap(cam, 10, 'after timeouts')
    check('camera works again after the timeouts', img is not None)

    # 6. hardware trigger settings (no pulses are sent) and the measured rolling-shutter readout
    nmap = cam.camera.GetNodeMap()
    for name in ('SensorShutterMode', 'AdcBitDepth', 'PixelFormat'):
        try:
            say(f'{name}: ' + PySpin.CEnumerationPtr(nmap.GetNode(name)).GetCurrentEntry().GetSymbolic())
        except Exception:
            pass
    old_const = lambda c: c.row_period_us * c.pixel_size_byte * (c.row_numbers - 1)
    for fmt in ([a.pixel_format] if a.quick else ['MONO8', a.pixel_format]):
        cam.set_pixel_format(fmt)
        r = cam.readout_us
        check(f'{fmt}: rolling-shutter readout measured on the camera', r is not None,
              f'{r / 1000:.2f} ms (IMX226 constants: {old_const(cam) / 1000:.2f} ms)' if r is not None else 'fallback to the IMX226 constants')
        frames.append(dict(test='readout', set_exposure_ms=None, got_image=None, total_s=None, camera_exposure_ms=None,
                           camera_frame_id=None, dtype=fmt, shape=None, mean=r))
    cam.set_hardware_triggered_acquisition()
    cam.set_exposure_time(10)
    hw_exp = PySpin.CFloatPtr(cam.camera.GetNodeMap().GetNode('ExposureTime')).GetValue()
    src = PySpin.CEnumerationPtr(cam.camera.GetNodeMap().GetNode('TriggerSource')).GetCurrentEntry().GetSymbolic()
    check('hardware trigger: source Line2', src == 'Line2', src)
    say(f'hardware trigger ({cam.pixel_format}): strobe delay {cam.strobe_delay_us / 1000:.2f} ms, camera exposure for 10 ms '
        f'illumination = {hw_exp / 1000:.1f} ms (light starts after the strobe delay, when all rows are exposing)')
    cam.set_software_triggered_acquisition()

    # 7. exposure and gain switching by software (today's channel switching, gain included)
    say('\n--- 7. exposure and gain switching by software')
    plan = [(12, 0.0), (22, 10.0), (100, 20.0)] * (3 if a.quick else 10)
    wrong_e, wrong_g, prev_g, missing = 0, 0, 0, 0
    rows7 = []
    for i, (e, g) in enumerate(plan):
        img, row = snap(cam, e, 'gain switching', gain=g)
        rows7.append(row)
        if img is None:
            missing += 1
            continue
        if not close_enough(row):
            wrong_e += 1
        if not gain_ok(row):
            wrong_g += 1
            if i > 0 and abs(row['camera_gain'] - plan[i - 1][1]) <= 0.2:
                prev_g += 1
    have_gain_chunk = any(r['camera_gain'] is not None for r in rows7)
    check(f'gain switching ({len(plan)} frames): every frame arrives', missing == 0, f'{missing} missing')
    check('gain switching: the camera reports the gain of each frame (chunk data)', have_gain_chunk,
          '' if have_gain_chunk else 'without it the gain of a frame cannot be checked')
    check('gain switching: every frame has the gain set for it', wrong_g == 0,
          f'{wrong_g} wrong, {prev_g} of them with the gain of the previous channel' if wrong_g else '')
    check('gain switching: every frame has the exposure set for it', wrong_e == 0, f'{wrong_e} wrong' if wrong_e else '')
    w = [r['gain_write_s'] for r in rows7 if r['gain_write_s'] is not None]
    over = [r['total_s'] - r['set_exposure_ms'] / 1000.0 for r in rows7 if r['got_image']]
    if w:
        say(f'INFO  writing the gain takes {1000 * sum(w) / len(w):.1f} ms on average (max {1000 * max(w):.1f} ms)')
    if over:
        say(f'INFO  trigger -> image, minus the exposure: {1000 * sum(over) / len(over):.1f} ms on average (readout + transfer)')

    # 8. Sequencer
    say('\n--- 8. Sequencer')
    seq_nodes = ['SequencerMode', 'SequencerConfigurationMode', 'SequencerConfigurationValid', 'SequencerSetSelector',
                 'SequencerSetValid', 'SequencerSetSave', 'SequencerSetLoad', 'SequencerSetStart', 'SequencerSetActive',
                 'SequencerPathSelector', 'SequencerSetNext', 'SequencerTriggerSource', 'SequencerTriggerActivation',
                 'SequencerFeatureSelector', 'SequencerFeatureEnable']
    for name in seq_nodes + ['ExposureMode', 'ChunkSelector', 'TriggerSelector', 'TriggerOverlap']:
        say(f'  {name}: {describe(name)}')
    if gnode('SequencerFeatureSelector') is not None:
        sel = PySpin.CEnumerationPtr(gnode('SequencerFeatureSelector'))
        for e in sel.GetEntries():
            try:
                fname = PySpin.CEnumEntryPtr(e).GetSymbolic()
                sel.SetIntValue(PySpin.CEnumEntryPtr(e).GetValue())
                say(f'  sequencer feature {fname}: enabled={gget("SequencerFeatureEnable")}')
            except Exception:
                pass
    required = ['SequencerMode', 'SequencerConfigurationMode', 'SequencerSetSelector', 'SequencerSetSave',
                'SequencerSetStart', 'SequencerSetNext', 'SequencerTriggerSource']
    missing_nodes = [n for n in required if gnode(n) is None]
    if missing_nodes:
        say('INFO  this camera has no (complete) Sequencer, missing: ' + ', '.join(missing_nodes)
            + ' -> multi-channel hardware triggering would need the same gain per sequence (or software gain switching)')
    else:
        say('INFO  the camera has the Sequencer nodes')
        seq_plan = [(10.0, 0.0), (25.0, 10.0), (50.0, 20.0)]

        def seq_configure(plan_, only_set=None):
            if gget('SequencerMode') == 'On':
                gset('SequencerMode', 'Off')
            gset('SequencerConfigurationMode', 'On')
            if gnode('SequencerFeatureSelector') is not None and gnode('SequencerFeatureEnable') is not None:
                for f in ('ExposureTime', 'Gain'):
                    try:
                        gset('SequencerFeatureSelector', f)
                        if not gget('SequencerFeatureEnable') and PySpin.IsWritable(gnode('SequencerFeatureEnable')):
                            gset('SequencerFeatureEnable', True)
                    except Exception as ex:
                        say(f'  (feature {f}: {ex})')
            for i, (e, g) in enumerate(plan_):
                if only_set is not None and i != only_set:
                    continue
                gset('SequencerSetSelector', i)
                if only_set is not None and gnode('SequencerSetLoad') is not None:
                    gset('SequencerSetLoad')
                gset('ExposureTime', e * 1000.0)
                gset('Gain', g)
                gset('SequencerPathSelector', 0)
                gset('SequencerTriggerSource', 'FrameStart')
                gset('SequencerSetNext', (i + 1) % len(plan_))
                gset('SequencerSetSave')
            gset('SequencerSetStart', 0)
            gset('SequencerConfigurationMode', 'Off')
            return gget('SequencerConfigurationValid')

        def seq_frames(plan_, n, label):
            bad_e, bad_g, bad_set, miss, rows = 0, 0, 0, 0, []
            for k in range(n):
                i = k % len(plan_)
                e, g = plan_[i]
                img, row = snap(cam, e, label, gain=g, configure=False, expected_set=i)
                rows.append(row)
                if img is None:
                    miss += 1
                    continue
                bad_e += not close_enough(row)
                bad_g += not gain_ok(row)
                if row['camera_sequencer_set'] is not None and row['camera_sequencer_set'] != i:
                    bad_set += 1
            return bad_e, bad_g, bad_set, miss, rows

        try:
            cam.set_software_triggered_acquisition()
            cam.set_exposure_time(10)            # also switches ExposureAuto / GainAuto off (needed for the Sequencer)
            cam.set_analog_gain(0)
            t0 = time.time()
            cam.stop_streaming()
            valid = seq_configure(seq_plan)
            t_cfg = time.time() - t0
            check('Sequencer: 3 sets (10 ms/gain 0, 25 ms/gain 10, 50 ms/gain 20) accepted', valid in ('Yes', None),
                  f'SequencerConfigurationValid = {valid}')
            gset('SequencerMode', 'On')
            cam.exposure_time = max(e for e, _ in seq_plan)       # read_frame timeout only
            cam.start_streaming()
            t_all = time.time() - t0
            check('Sequencer: SequencerMode is On while streaming', gget('SequencerMode') == 'On', str(gget('SequencerMode')))
            say(f'INFO  configuring 3 sets: {1000 * t_cfg:.0f} ms (incl. stop streaming); until streaming again: {1000 * t_all:.0f} ms')
            say(f'INFO  ExposureTime writable while the Sequencer runs: {PySpin.IsWritable(gnode("ExposureTime"))}; '
                f'Gain writable: {PySpin.IsWritable(gnode("Gain"))}')
            n = len(seq_plan) * (3 if a.quick else 10)
            bad_e, bad_g, bad_set, miss, rows = seq_frames(seq_plan, n, 'sequencer')
            check(f'Sequencer ({n} frames): every frame arrives', miss == 0, f'{miss} missing')
            check('Sequencer: every frame has the exposure of its set (in order, starting with set 0)', bad_e == 0,
                  f'{bad_e} wrong; first frames: ' + ', '.join(str(r['camera_exposure_ms']) for r in rows[:4]) if bad_e else '')
            check('Sequencer: every frame has the gain of its set', bad_g == 0,
                  f'{bad_g} wrong; first frames: ' + ', '.join(str(r['camera_gain']) for r in rows[:4]) if bad_g else '')
            if any(r['camera_sequencer_set'] is not None for r in rows):
                check('Sequencer: the camera reports the set number of each frame (chunk data), in order', bad_set == 0,
                      f'{bad_set} wrong')
            else:
                say('INFO  the camera does not report the set number per frame (chunk SequencerSetActive)')
            over = [r['total_s'] - r['set_exposure_ms'] / 1000.0 for r in rows if r['got_image']]
            if over:
                say(f'INFO  Sequencer: trigger -> image, minus the exposure: {1000 * sum(over) / len(over):.1f} ms on average')

            # hardware trigger with the Sequencer on (no pulses are sent): can the source be switched?
            try:
                gset('TriggerSource', 'Line2')
                src = gget('TriggerSource')
                gset('TriggerSource', 'Software')
                check('Sequencer: trigger source can be switched to Line2 (hardware trigger) while it runs', src == 'Line2', str(src))
            except Exception as ex:
                check('Sequencer: trigger source can be switched to Line2 (hardware trigger) while it runs', False, repr(ex))
                try:
                    gset('TriggerSource', 'Software')
                except Exception:
                    pass

            # change one set (as when a channel's gain is edited): how long, and is it used?
            t0 = time.time()
            cam.stop_streaming()
            seq_plan2 = [seq_plan[0], (25.0, 5.0), seq_plan[2]]
            valid = seq_configure(seq_plan2, only_set=1)
            gset('SequencerMode', 'On')
            cam.start_streaming()
            t_change = time.time() - t0
            bad_e, bad_g, bad_set, miss, rows = seq_frames(seq_plan2, len(seq_plan2) * 2, 'sequencer changed set')
            check('Sequencer: after changing set 1 (gain 10 -> 5) every frame has the exposure and gain of its set',
                  miss == 0 and bad_e == 0 and bad_g == 0, f'{miss} missing, {bad_e} wrong exposure, {bad_g} wrong gain, valid={valid}')
            say(f'INFO  changing one set (stop, configure, start): {1000 * t_change:.0f} ms')
        except Exception as ex:
            check('Sequencer test ran without error', False, repr(ex))
            for ln in traceback.format_exc().splitlines():
                say('    ' + ln)
        finally:
            try:
                cam.stop_streaming()
                if gget('SequencerMode') == 'On':
                    gset('SequencerMode', 'Off')
                if gget('SequencerConfigurationMode') == 'On':
                    gset('SequencerConfigurationMode', 'Off')
                if gget('TriggerSource') != 'Software':
                    gset('TriggerSource', 'Software')
            except Exception as ex:
                say('  error while switching the Sequencer off: ' + repr(ex))
            cam.start_streaming()
            check('Sequencer is switched off at the end', gget('SequencerMode') in ('Off', None), str(gget('SequencerMode')))
            cam.set_analog_gain(10)
            img, row = snap(cam, 30, 'after sequencer', gain=10)
            check('after the Sequencer test a normal frame has the exposure and gain set for it',
                  img is not None and close_enough(row) and gain_ok(row),
                  f'camera {row["camera_exposure_ms"]} ms, gain {row["camera_gain"]}')

    try:
        cam.stop_streaming()
        cam.camera.DeInit()
    except Exception:
        pass
    n_fail = sum(1 for _, ok in results if not ok)
    say(f'\n{len(results) - n_fail} passed, {n_fail} failed')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    with open(frames_path, 'w', newline='', encoding='utf-8') as f:
        keys = []
        for fr in frames:
            keys += [k for k in fr if k not in keys]
        w = csv.DictWriter(f, fieldnames=keys, restval='')
        w.writeheader()
        w.writerows(frames)
    print('report:', report_path)
    print('frames:', frames_path)
    sys.stdout.flush()
    os._exit(1 if n_fail else 0)


if __name__ == '__main__':
    main()
