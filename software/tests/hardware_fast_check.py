"""Check fast (video-rate) recording of the FLIR camera on the microscope computer: Camera.fast_acquire in control/camera_flir.py.

Only the camera is used: the stage does not move and no illumination is switched on (frames are dark; frame numbers and camera clock are checked).
Close the Squid software first (the camera can only be opened by one program).

    pixi run python tests/hardware_fast_check.py                 # about 1 minute
    python tests/hardware_fast_check.py --fake                   # without camera (fake PySpin, to test this script)

Writes fast_check_<date>.txt next to this file; send it. For 8 and 16 bit and two regions it reports
  * the highest frame rate the camera accepts (exposure 5 ms) and the rate it actually delivered, lost frames
  * a 5 s recording at 25 frames/s: achieved rate, interval statistics (mean, sd, max), lost frames, incomplete frames, timeouts
  * that the camera settings are the same afterwards (pixel format, region, trigger mode, exposure, gain)
and the USB buffer limit of the computer (usbfs_memory_mb, should be 1000).
"""
import os
import sys
import time
import argparse
import datetime

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SW = os.path.dirname(HERE)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fake', action='store_true')
    ap.add_argument('--out', default=HERE)
    ap.add_argument('--fps', type=float, default=25)
    ap.add_argument('--seconds', type=float, default=5)
    ap.add_argument('--rois', default='2000,1000')
    a = ap.parse_args()
    os.chdir(SW)
    sys.path.insert(0, SW)
    if a.fake:
        sys.path.insert(0, HERE)
        import fake_pyspin
        sys.modules['PySpin'] = fake_pyspin
        fake_pyspin.reset().paced = True
    import control.camera_flir as cf
    from control.jobs.hardware_backend import usbfs_memory_mb

    lines = []

    def say(t=''):
        print(t)
        lines.append(t)
    say(f'FLIR fast recording check, {datetime.datetime.now().isoformat(timespec="seconds")}')
    mb = usbfs_memory_mb()
    say(f'usbfs_memory_mb: {mb}' + ('' if mb is None or mb >= 200 else '   <-- too small for fast recording; set usbcore.usbfs_memory_mb=1000 (GRUB)'))
    cam = cf.Camera(sn=None)
    cam.open()
    cam.set_pixel_format('MONO16')
    cam.set_software_triggered_acquisition()
    cam.start_streaming()

    def settings():
        return (cam.pixel_format, cam.trigger_mode, cam.exposure_time, cam.analog_gain, cam.is_streaming,
                tuple(cf.PySpin.CIntegerPtr(cam.camera.GetNodeMap().GetNode(k)).GetValue() for k in ('OffsetX', 'OffsetY', 'Width', 'Height')))
    before = settings()
    say(f'camera settings before: {before}')
    bad = 0
    for bits in (8, 16):
        for roi in [int(r) for r in a.rois.split(',')]:
            say(f'\n=== {bits} bit, {roi} x {roi} ===')
            try:
                out = cam.fast_acquire(120, fps=190, bits=bits, roi=roi, exposure_ms=5, gain=0)   # 190 fps = more than the camera can do: it limits itself
                ts = np.asarray(out['timestamps_ns'], dtype=float) / 1e9
                got = 1 / np.diff(ts).mean() if len(ts) > 2 else float('nan')
                lost = int(np.clip(np.diff(out['frame_ids']) - 1, 0, None).sum())
                say(f'highest rate: camera accepted {out["fps_set"]} fps, delivered {got:.1f} fps, region {out["roi"]}, {len(ts)} frames, lost {lost}, incomplete {out["incomplete"]}, timeouts {out["timeouts"]}')
                n = int(a.fps * a.seconds)
                out = cam.fast_acquire(n, fps=a.fps, bits=bits, roi=roi, exposure_ms=5, gain=0)
                ts = np.asarray(out['timestamps_ns'], dtype=float) / 1e9
                d = np.diff(ts)
                lost = int(np.clip(np.diff(out['frame_ids']) - 1, 0, None).sum())
                say(f'{a.fps:g} fps for {a.seconds:g} s: {len(ts)} of {n} frames, achieved {1 / d.mean():.2f} fps, interval mean {d.mean() * 1000:.2f} ms, sd {d.std() * 1000:.2f}, '
                    f'max {d.max() * 1000:.1f} ms, lost {lost}, incomplete {out["incomplete"]}, timeouts {out["timeouts"]}, buffers {out["buffers"]}')
                if lost or out['incomplete'] or out['timeouts'] or abs(1 / d.mean() - a.fps) > 0.05 * a.fps:
                    bad += 1
                    say('   PROBLEM: frames lost or rate not kept')
                if out['restore_problems']:
                    bad += 1
                    say('   PROBLEM: not restored: ' + str(out['restore_problems']))
            except Exception as e:
                bad += 1
                say(f'   ERROR: {e!r}')
            same = settings() == before
            say('camera settings restored: ' + ('yes' if same else f'NO: {settings()}'))
            bad += 0 if same else 1
    say(f'\n{"ALL OK" if not bad else str(bad) + " problem(s)"}')
    path = os.path.join(a.out, f'fast_check_{datetime.datetime.now():%Y%m%d_%H%M%S}.txt')
    with open(path, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    print('report:', path)
    try:
        cam.close()
    except Exception:
        pass
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
