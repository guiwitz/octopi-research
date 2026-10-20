"""Exposure carry-over: the FLIR camera reported, for every frame, the exposure that was set BEFORE the previous frame (hardware_flir_check, 20261020).
This script tries ways of switching the exposure and reports for each one how many frames carry the exposure that was set for them
(read back from the camera's own frame information, "chunk data"). Only the camera is used: no stage movement, no light. Close Squid first.

    pixi run python tests/hardware_exposure_lag_check.py            # about 1 minute
    python tests/hardware_exposure_lag_check.py --fake              # without camera (fake PySpin with the same one-frame lag), to test this script

Writes exposure_lag_<date>.txt next to this script; send it. Variants (each: exposures 12, 22, 100 ms, repeated, software trigger, MONO16):
  V0  baseline: set exposure, trigger, read          (what the software does now)
  V1  set the exposure twice, then trigger
  V2  set exposure, read the value back from the camera, trigger
  V3  set exposure, wait 0.3 s, trigger              (is it a matter of time?)
  V4  read a frame, wait 0.3 s, then set exposure and trigger
  V5  pre-set: right after triggering frame N, write the exposure for frame N+1, then read frame N
  V6  set exposure, trigger and read a throw-away frame (nothing lit), trigger and read the real one      (costs one extra frame)
  V7  set exposure, stop and restart streaming, trigger
  V8  TriggerOverlap = ReadOut, then like V0 (if the camera allows it)
  V9  set exposure with TriggerMode off and on again around the write (if allowed while streaming)
  V10 set exposure in the middle of a throw-away trigger (set, trigger, set again, read)
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
    if a.fake:
        sys.path.insert(0, HERE)
        import fake_pyspin
        sys.modules['PySpin'] = fake_pyspin
        fake_pyspin.reset().exposure_lag = True
    import PySpin
    import control.camera_flir as cf

    lines = []

    def say(t=''):
        print(t, flush=True)
        lines.append(t)

    say(f'FLIR exposure lag check, {datetime.datetime.now().isoformat(timespec="seconds")}  ({"fake" if a.fake else "real camera"})')
    cam = cf.Camera(sn=None)
    cam.open()
    cam.set_pixel_format(a.pixel_format)
    cam.set_software_triggered_acquisition()
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

    plan = [12.0, 22.0, 100.0] * a.cycles

    def get():
        """trigger-less read of one frame; -> reported exposure (ms) or None"""
        last.clear()
        img = cam.read_frame()
        return last.get('exp') if img is not None else None

    def trig_read():
        cam.send_trigger()
        return get()

    def set_exp(e):
        cam.set_exposure_time(e)

    def readback():
        return PySpin.CFloatPtr(cam.camera.GetNodeMap().GetNode('ExposureTime')).GetValue()

    def ok(rep, want):
        return rep is not None and abs(rep - want) <= max(0.05, 0.05 * want)

    results = {}

    def v0():
        out = []
        for e in plan:
            set_exp(e)
            out.append((e, trig_read()))
        return out

    def v1():
        out = []
        for e in plan:
            set_exp(e); set_exp(e)
            out.append((e, trig_read()))
        return out

    def v2():
        out = []
        for e in plan:
            set_exp(e); readback()
            out.append((e, trig_read()))
        return out

    def v3():
        out = []
        for e in plan:
            set_exp(e); time.sleep(0.3)
            out.append((e, trig_read()))
        return out

    def v4():
        out = []
        for e in plan:
            time.sleep(0.3)
            set_exp(e)
            out.append((e, trig_read()))
        return out

    def v5():
        out = []
        set_exp(plan[0]); trig_read(); trig_read()               # settle
        set_exp(plan[0])
        for i, e in enumerate(plan):
            cam.send_trigger()
            if i + 1 < len(plan):
                set_exp(plan[i + 1])
            out.append((e, get()))
        return out

    def v6():
        out = []
        for e in plan:
            set_exp(e)
            trig_read()                                            # throw-away
            out.append((e, trig_read()))
        return out

    def v7():
        out = []
        for e in plan:
            set_exp(e)
            cam.stop_streaming(); cam.start_streaming()
            out.append((e, trig_read()))
        return out

    def v8():
        node = PySpin.CEnumerationPtr(cam.camera.GetNodeMap().GetNode('TriggerOverlap'))
        old = node.GetIntValue()
        try:
            node.SetIntValue(node.GetEntryByName('ReadOut').GetValue())
            return v0()
        finally:
            node.SetIntValue(old)

    def v9():
        node = PySpin.CEnumerationPtr(cam.camera.GetNodeMap().GetNode('TriggerMode'))
        off, on = node.GetEntryByName('Off').GetValue(), node.GetEntryByName('On').GetValue()
        out = []
        for e in plan:
            node.SetIntValue(off); set_exp(e); node.SetIntValue(on)
            out.append((e, trig_read()))
        return out

    def v10():
        out = []
        for e in plan:
            set_exp(e)
            cam.send_trigger()
            set_exp(e)
            out.append((e, get()))
        return out

    variants = [('V0', 'baseline: set, trigger, read (now)', v0), ('V1', 'set twice', v1), ('V2', 'set, read back, trigger', v2),
                ('V3', 'set, wait 0.3 s, trigger', v3), ('V4', 'wait 0.3 s, set, trigger', v4),
                ('V5', 'pre-set: write the NEXT exposure right after the trigger', v5), ('V6', 'throw-away frame after setting (one extra frame)', v6),
                ('V7', 'set, restart streaming, trigger', v7), ('V8', 'TriggerOverlap=ReadOut, then baseline', v8),
                ('V9', 'TriggerMode off/on around the write', v9), ('V10', 'set, trigger, set again, read', v10)]
    summary = []
    for key, text, fn in variants:
        cam.set_exposure_time(12.0); trig_read(); trig_read()      # settle: same state before every variant
        t0 = time.time()
        try:
            out = fn()
        except Exception as ex:
            say(f'{key} {text}: not possible here ({ex!r})')
            summary.append((key, text, None, None))
            try:
                cam.stop_streaming(); cam.start_streaming()
            except Exception:
                pass
            continue
        dt = (time.time() - t0) / len(out)
        good = sum(1 for e, r in out if ok(r, e))
        prev = sum(1 for i, (e, r) in enumerate(out) if i and not ok(r, e) and ok(r, out[i - 1][0]))
        none = sum(1 for e, r in out if r is None)
        say(f'{key} {text}: {good}/{len(out)} frames with the right exposure; {prev} wrong ones carry the previous frame\'s exposure; {none} without image; {dt * 1000:.0f} ms per frame')
        say('      set     : ' + ' '.join(f'{e:7.0f}' for e, r in out[:9]))
        say('      reported: ' + ' '.join(f'{(r if r is not None else float("nan")):7.1f}' for e, r in out[:9]))
        summary.append((key, text, good == len(out), dt))
    say('\nSUMMARY')
    for key, text, allok, dt in summary:
        say(f'  {key:4} {"RIGHT EVERY TIME" if allok else ("not possible" if allok is None else "wrong"):17} {text}' + (f'  ({dt * 1000:.0f} ms per frame)' if dt else ''))
    path = os.path.join(a.out, f'exposure_lag_{datetime.datetime.now():%Y%m%d_%H%M%S}.txt')
    open(path, 'w').write('\n'.join(lines) + '\n')
    print('report:', path)
    try:
        cam.close()
    except Exception:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
