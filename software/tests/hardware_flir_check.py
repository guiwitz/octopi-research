"""Check the FLIR camera and the FLIR driver (control/camera_flir.py) on the microscope computer.

Only the camera is used: the stage does not move and no illumination is switched on (images are dark; the check
uses the exposure the camera reports for each frame - "chunk data" - not the image brightness).
Close the Squid software first (the camera can only be opened by one program).

Run in the software folder of the branch, with its pixi environment:
    pixi run python tests/hardware_flir_check.py                 # about 2 minutes
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
"""
import os
import sys
import csv
import time
import argparse
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
SW = os.path.dirname(HERE)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fake', action='store_true', help='use the fake PySpin (no camera needed)')
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
        fake_pyspin.reset()
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
            for name in ('ExposureTime', 'FrameID', 'Timestamp', 'Gain'):
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
            return img

    def wrap_get_next_image(cam):
        cam.camera = CameraProxy(cam.camera)

    def snap(cam, exposure_ms, label):
        cam.set_exposure_time(exposure_ms)
        t0 = time.time()
        cam.send_trigger()
        img = cam.read_frame()
        dt = time.time() - t0
        row = dict(test=label, set_exposure_ms=exposure_ms, got_image=img is not None, total_s=round(dt, 3),
                   camera_exposure_ms=last.get('chunk_exposure_ms'), camera_frame_id=last.get('chunk_frame_id'),
                   dtype=getattr(img, 'dtype', None), shape=getattr(img, 'shape', None),
                   mean=float(img.mean()) if img is not None else None)
        frames.append(row)
        return img, row

    def close_enough(row, tol=0.05):
        e = row['camera_exposure_ms']
        return e is None or abs(e - row['set_exposure_ms']) <= max(0.05, tol * row['set_exposure_ms'])

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
        w = csv.DictWriter(f, fieldnames=list(frames[0].keys()))
        w.writeheader()
        w.writerows(frames)
    print('report:', report_path)
    print('frames:', frames_path)
    sys.stdout.flush()
    os._exit(1 if n_fail else 0)


if __name__ == '__main__':
    main()
