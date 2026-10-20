"""Exposure carry-over in HARDWARE trigger mode (hardware_exposure_lag_check.py only tested software triggers).
In hardware trigger mode the camera is triggered by a pulse from the microcontroller (Line2). This script sends such pulses
(no illumination is switched on, the stage does not move) and reads from the camera's own frame information ("chunk data")
which exposure each frame really had. Close Squid first (camera and microcontroller can only be used by one program).

    pixi run python tests/hardware_trigger_lag_check.py            # about 1 minute
    python tests/hardware_trigger_lag_check.py --fake              # without hardware (fake PySpin + fake pulses), to test this script

Writes hardware_trigger_lag_<date>.txt next to this script; send it.

In hardware trigger mode the camera exposure is the illumination time plus the rolling-shutter readout, so the value that was
written to the camera is read back right after set_exposure_time and each frame is compared with it.
Variants (exposures 12, 22, 100 ms illumination, repeated; MONO16):
  H0  baseline: set exposure, pulse, read          (what the software does now in hardware trigger mode)
  H1  set exposure, wait 0.3 s, pulse, read        (is it a matter of time?)
  H2  set exposure, pulse + read a throw-away frame (dark), pulse + read the real one     (costs one extra frame)
  H3  control: the same exposure every time        (frames must all carry it)
"""
import os
import sys
import time
import argparse
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
SW = os.path.dirname(HERE)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fake', action='store_true')
    ap.add_argument('--out', default=HERE)
    ap.add_argument('--pixel-format', default='MONO16')
    ap.add_argument('--cycles', type=int, default=6)
    a = ap.parse_args()
    os.chdir(SW)
    sys.path.insert(0, SW)
    fake = None
    if a.fake:
        sys.path.insert(0, HERE)
        import fake_pyspin
        sys.modules['PySpin'] = fake_pyspin
        fake = fake_pyspin.reset()
        fake.exposure_lag = True
    import PySpin
    import control.camera_flir as cf

    lines = []

    def say(t=''):
        print(t, flush=True)
        lines.append(t)

    say(f'FLIR hardware trigger exposure lag check, {datetime.datetime.now().isoformat(timespec="seconds")}  ({"fake" if a.fake else "real camera"})')

    # microcontroller (sends the trigger pulses)
    mcu = None
    if not a.fake:
        try:
            from control.microcontroller import Microcontroller
            from control._def import CONTROLLER_VERSION, CONTROLLER_SN
            mcu = Microcontroller(version=CONTROLLER_VERSION, sn=CONTROLLER_SN)
            time.sleep(1.0)
        except Exception as ex:
            say('ERROR: microcontroller not available (' + str(ex) + '); hardware trigger pulses cannot be sent')
            return 2

    cam = cf.Camera(sn=None)
    cam.open()
    cam.set_pixel_format(a.pixel_format)
    nm = cam.camera.GetNodeMap()
    try:
        PySpin.CBooleanPtr(nm.GetNode('ChunkModeActive')).SetValue(True)
        sel = PySpin.CEnumerationPtr(nm.GetNode('ChunkSelector'))
        for name in ('ExposureTime', 'FrameID'):
            e = sel.GetEntryByName(name)
            if e is not None and PySpin.IsReadable(e):
                sel.SetIntValue(e.GetValue())
                PySpin.CBooleanPtr(nm.GetNode('ChunkEnable')).SetValue(True)
    except Exception as ex:
        say('ERROR: chunk data not available (' + str(ex) + '); this check cannot work')
        return 2
    cam.exposure_lag_fix = False
    cam.set_hardware_triggered_acquisition()
    cam.start_streaming()
    last = {}

    class Proxy:
        def __init__(self, real):
            self._real = real

        def __getattr__(self, n):
            return getattr(self._real, n)

        def GetNextImage(self, *args):
            img = self._real.GetNextImage(*args)
            try:
                last['exp'] = img.GetChunkData().GetExposureTime() / 1000.0
                last['id'] = img.GetChunkData().GetFrameID()
            except Exception:
                last['exp'] = None
            return img
    cam.camera = Proxy(cam.camera)

    def readback():
        return PySpin.CFloatPtr(cam.camera.GetNodeMap().GetNode('ExposureTime')).GetValue() / 1000.0

    def pulse(ill_ms):
        if fake is not None:
            fake.hardware_trigger('Line2')
        else:
            mcu.send_hardware_trigger(control_illumination=False, illumination_on_time_us=ill_ms * 1000)

    def pulse_read(ill_ms):
        """one pulse and the frame it produces -> reported exposure (ms) or None"""
        last.clear()
        pulse(ill_ms)
        img = cam.read_frame()
        return last.get('exp') if img is not None else None

    def ok(rep, want):
        return rep is not None and abs(rep - want) <= max(0.05, 0.03 * want)

    plan = [12.0, 22.0, 100.0] * a.cycles
    results = {}

    def run(key, title, body, plan=plan):
        want, rep, missing = [], [], 0
        for e in plan:
            try:
                w, r = body(e)
            except Exception as ex:
                say(f'   {key}: error {ex}')
                w, r = None, None
            want.append(w)
            rep.append(r)
            time.sleep(0.05)
        n = len(plan)
        right = sum(1 for w, r in zip(want, rep) if w is not None and ok(r, w))
        none = sum(1 for r in rep if r is None)
        prev = sum(1 for i in range(1, n) if want[i] is not None and not ok(rep[i], want[i]) and want[i - 1] is not None and ok(rep[i], want[i - 1]))
        say(f'{key} {title}: {right}/{n} frames with the right exposure; {prev} wrong ones carry the previous frame\'s exposure; {none} without image')
        f = lambda v: '   none' if v is None else '%7.1f' % v
        say('      written :' + ''.join(f(w) for w in want[:9]))
        say('      reported:' + ''.join(f(r) for r in rep[:9]))
        results[key] = (right == n, title, right, n)

    def h0(e):
        cam.set_exposure_time(e)
        w = readback()
        return w, pulse_read(e)

    def h1(e):
        cam.set_exposure_time(e)
        w = readback()
        time.sleep(0.3)
        return w, pulse_read(e)

    def h2(e):
        cam.set_exposure_time(e)
        w = readback()
        pulse_read(e)                 # throw-away frame (dark)
        return w, pulse_read(e)

    def h3(e):
        w = readback()
        return w, pulse_read(12.0)

    cam.set_exposure_time(12.0)
    pulse_read(12.0)
    pulse_read(12.0)
    try:
        run('H0', 'baseline: set, pulse, read (now)', h0)
        run('H1', 'set, wait 0.3 s, pulse', h1)
        run('H2', 'throw-away pulse + frame after setting (one extra frame)', h2)
        cam.set_exposure_time(12.0)
        pulse_read(12.0)
        pulse_read(12.0)
        run('H3', 'control: same exposure every time', h3)
    finally:
        try:
            cam.set_software_triggered_acquisition()
        except Exception:
            pass
        if mcu is not None:
            try:
                mcu.close()
            except Exception:
                pass

    say('')
    say('SUMMARY')
    for k, (good, title, right, n) in results.items():
        say(f'  {k}  {"RIGHT EVERY TIME" if good else "wrong           "}  {title}  ({right}/{n})')
    if 'H0' in results and not results['H0'][0]:
        say('=> hardware trigger mode has the same one-frame exposure lag.' + ('  H2 (throw-away pulse) fixes it.' if results.get('H2', (False,))[0] else ''))
    elif 'H0' in results:
        say('=> no exposure lag in hardware trigger mode.')
    stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    path = os.path.join(a.out, f'hardware_trigger_lag_{stamp}.txt')
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
        print('report:', path)
    except Exception as ex:
        print('could not write the report:', ex)
    return 0


if __name__ == '__main__':
    sys.exit(main())
