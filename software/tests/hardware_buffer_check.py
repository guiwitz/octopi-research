"""Find out why large images with several illuminations sometimes crash with a camera buffer error (FLIR), and whether the disk is involved.

Only the camera and a disk are used: the stage does not move and no illumination is switched on (images are dark; what is checked is that every
frame arrives complete, how long the camera needs, and what the USB stream reports). Close the Squid software first.

Each scenario takes CYCLES cycles; a cycle = one image per "channel" (different exposures, software trigger, as in an acquisition):
  A  no saving                                  baseline: do errors happen without any disk activity?
  B  every image saved at once as its own TIFF  (the way the Flexible Multipoint tab writes single files; zlib, same function)
  C  images kept in memory, ONE multipage OME-TIFF written per cycle   (JOBs 'multipage' mode)
  D  like B, but the files are written by a second thread (the way the JOBs writer works) while the camera is triggered again
Run it for each disk you want to test (--save-dir on the HDD, then on the SSD) and send all the reports.

    pixi run python tests/hardware_buffer_check.py --save-dir D:\\bufcheck                 # about 5 min, 2000 x 2000, 4 channels
    pixi run python tests/hardware_buffer_check.py --save-dir E:\\bufcheck --size 0        # size 0 = full sensor
    pixi run python tests/hardware_buffer_check.py --save-dir D:\\bufcheck --cycles 100 --channels 5 --pixel-format MONO16
    python tests/hardware_buffer_check.py --fake --save-dir <folder>                      # without camera (to test this script)

Reports: buffer_check_<date>.txt next to this script (the files written for the test are deleted afterwards unless --keep).
Reported per scenario: images requested/received, None (no image), incomplete-image messages, exceptions, extra (stale) frames still waiting
after a read, read time (mean/max), write time and MB/s, and the counters of the USB stream (lost / failed / underrun frames) when the camera has them.
Also reported: usbfs_memory_mb, pixel format, size, free disk space.
"""
import os
import re
import sys
import time
import shutil
import argparse
import datetime
import threading
import traceback

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SW = os.path.dirname(HERE)

STREAM_COUNTERS = ('StreamLostFrameCount', 'StreamFailedBufferCount', 'StreamBufferUnderrunCount', 'StreamIncompleteFrameCount',
                   'StreamDeliveredFrameCount', 'StreamBufferCountResult', 'StreamBufferCountMax')


class Tee:
    """copy of what the camera driver prints, to count its error messages"""
    def __init__(self, out):
        self.out, self.lines = out, []

    def write(self, s):
        self.out.write(s)
        if s.strip():
            self.lines.append(s.strip())

    def flush(self):
        self.out.flush()

    def count(self, pat):
        return sum(1 for l in self.lines if re.search(pat, l))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fake', action='store_true')
    ap.add_argument('--save-dir', required=True, help='folder on the disk to test (a subfolder is made and removed)')
    ap.add_argument('--size', type=int, default=2000, help='square region in pixels; 0 = full sensor')
    ap.add_argument('--channels', type=int, default=4)
    ap.add_argument('--cycles', type=int, default=30)
    ap.add_argument('--exposures', default='10,50,100,20,30', help='ms, one per channel (repeated if fewer)')
    ap.add_argument('--pixel-format', default='MONO16')
    ap.add_argument('--scenarios', default='A,B,C,D')
    ap.add_argument('--keep', action='store_true')
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
    from control.towbin_funs import save_single_plane_tiff
    from control.jobs import stackio
    from control.jobs.hardware_backend import usbfs_memory_mb

    tee = Tee(sys.stdout)
    sys.stdout = tee
    lines = []

    def say(t=''):
        tee.out.write(t + '\n')
        lines.append(t)

    folder = os.path.join(a.save_dir, 'bufcheck_' + datetime.datetime.now().strftime('%H%M%S'))
    os.makedirs(folder, exist_ok=True)
    say(f'FLIR large-image buffer check, {datetime.datetime.now().isoformat(timespec="seconds")}')
    say(f'save dir {folder}; free disk space {shutil.disk_usage(folder).free / 1e9:.0f} GB')
    mb = usbfs_memory_mb()
    say(f'usbfs_memory_mb: {mb}' + ('' if mb is None or mb >= 200 else '   <-- small; large frames may fail. Linux fix: usbcore.usbfs_memory_mb=1000 (GRUB)'))
    cam = cf.Camera(sn=None)
    cam.open()
    cam.set_pixel_format(a.pixel_format)
    if a.size:
        cam.set_ROI(0, 0, a.size, a.size)
    cam.set_software_triggered_acquisition()
    cam.start_streaming()
    exps = [float(x) for x in a.exposures.split(',')]
    exps = [exps[i % len(exps)] for i in range(a.channels)]
    names = [f'ch{i}_{e:g}ms' for i, e in enumerate(exps)]
    cam.set_exposure_time(exps[0])
    first = None
    for _ in range(3):
        cam.send_trigger()
        first = cam.read_frame()
        if first is not None:
            break
    if first is None:
        say('ERROR: the camera delivers no image at all; nothing to test')
        return 2
    say(f'pixel format {cam.pixel_format}, image {first.shape[1]} x {first.shape[0]} {first.dtype}, {first.nbytes / 1e6:.1f} MB per image; '
        f'channels (exposures): {exps}; {a.cycles} cycles = {a.cycles * a.channels} images per scenario')

    def stream_counters():
        out = {}
        try:
            tl = cam.camera.GetTLStreamNodeMap()
            for n in STREAM_COUNTERS:
                node = PySpin.CIntegerPtr(tl.GetNode(n))
                if node is not None and PySpin.IsReadable(node):
                    out[n] = node.GetValue()
        except Exception:
            pass
        return out

    bad_total = 0

    def run(label, title, save):
        nonlocal bad_total
        say(f'\n=== {label}: {title} ===')
        c0, n_inc0, n_no0 = stream_counters(), tee.count(r'incomplete'), tee.count(r'no image')
        stats = dict(req=0, ok=0, none=0, exc=0, stale=0, read=[], write=[], bytes=0)
        t_start = time.time()
        writer_q, writer_t, writer_err = None, None, []
        if save == 'thread':
            import queue
            writer_q = queue.Queue()

            def work():
                while True:
                    it = writer_q.get()
                    if it is None:
                        return
                    try:
                        t = time.time()
                        save_single_plane_tiff(it[0], it[1], channels=[it[2]])
                        stats['write'].append(time.time() - t)
                        stats['bytes'] += it[0].nbytes
                    except Exception as e:
                        writer_err.append(repr(e))
            writer_t = threading.Thread(target=work, daemon=True)
            writer_t.start()
        for cyc in range(a.cycles):
            kept = []
            for ci in range(a.channels):
                stats['req'] += 1
                cam.set_exposure_time(exps[ci])
                t = time.time()
                try:
                    cam.send_trigger()
                    img = cam.read_frame()
                except Exception as e:
                    stats['exc'] += 1
                    say(f'   cycle {cyc} {names[ci]}: EXCEPTION {e!r}')
                    continue
                stats['read'].append(time.time() - t)
                if img is None:
                    stats['none'] += 1
                    say(f'   cycle {cyc} {names[ci]}: no image')
                    continue
                stats['ok'] += 1
                try:                                    # a frame that is still waiting would be read for the NEXT trigger (stale image)
                    extra = cam.camera.GetNextImage(1)
                    extra.Release()
                    stats['stale'] += 1
                except Exception:
                    pass
                path = os.path.join(folder, f'{label}_{cyc:04d}_{names[ci]}.tiff')
                if save == 'single':
                    t = time.time()
                    save_single_plane_tiff(img, path, channels=[names[ci]])
                    stats['write'].append(time.time() - t)
                    stats['bytes'] += img.nbytes
                elif save == 'thread':
                    writer_q.put((img.copy(), path, names[ci]))
                elif save == 'multipage':
                    kept.append(img)
            if save == 'multipage' and kept:
                t = time.time()
                data = np.stack(kept)[None, None]
                stackio.write_ome_tiff(os.path.join(folder, f'{label}_{cyc:04d}.tiff'), data, names[:len(kept)])
                stats['write'].append(time.time() - t)
                stats['bytes'] += data.nbytes
        if writer_q is not None:
            writer_q.put(None)
            writer_t.join()
        dur = time.time() - t_start
        r = np.array(stats['read']) if stats['read'] else np.zeros(1)
        w = np.array(stats['write']) if stats['write'] else None
        inc, no = tee.count(r'incomplete') - n_inc0, tee.count(r'no image') - n_no0
        say(f'images requested {stats["req"]}, received {stats["ok"]}, none {stats["none"]}, exceptions {stats["exc"]}, incomplete-image messages {inc}, '
            f'stale frames {stats["stale"]}, driver "no image" messages {no}')
        say(f'read time (trigger to image): mean {r.mean() * 1000:.0f} ms, max {r.max() * 1000:.0f} ms; scenario took {dur:.0f} s')
        if w is not None:
            say(f'writing: {len(w)} file(s), mean {w.mean():.2f} s, max {w.max():.2f} s, {stats["bytes"] / 1e6 / max(w.sum(), 1e-9):.0f} MB/s while writing')
        if writer_err:
            say(f'   writer errors: {writer_err[:3]}')
        c1 = stream_counters()
        diff = {k: c1[k] - c0.get(k, 0) for k in c1 if k not in ('StreamBufferCountResult', 'StreamBufferCountMax')}
        if c1:
            say(f'USB stream counters (change during this scenario): {diff}   (buffers: {c1.get("StreamBufferCountResult")}/{c1.get("StreamBufferCountMax")})')
        else:
            say('USB stream counters: not available on this camera/driver')
        problems = stats['none'] + stats['exc'] + inc + no + stats['stale'] + len(writer_err) + sum(v for k, v in diff.items() if 'Lost' in k or 'Failed' in k or 'Underrun' in k or 'Incomplete' in k)
        if problems:
            bad_total += 1
            say(f'   PROBLEMS in this scenario: {problems}')
        return problems

    todo = {'A': ('no saving', None), 'B': ('each image saved at once as a single TIFF', 'single'),
            'C': ('images kept in memory, one multipage file per cycle', 'multipage'),
            'D': ('single TIFFs written by a second thread while the camera goes on', 'thread')}
    try:
        for k in a.scenarios.split(','):
            k = k.strip().upper()
            if k in todo:
                run(k, *todo[k])
    except Exception:
        say('ERROR during the test:\n' + traceback.format_exc())
        bad_total += 1
    finally:
        sys.stdout = tee.out
        if not a.keep:
            shutil.rmtree(folder, ignore_errors=True)
    say('\nHOW TO READ: problems in A (no disk) point to the camera/USB side (usbfs_memory_mb, cable, port, hub, power saving); problems only in B/C/D, and more on the HDD '
        'than on the SSD, point to the host being stalled by the disk (then the stream buffer overflows). "stale frames" = a frame arrived that nobody asked for: the next image '
        'would carry the wrong exposure (the exposure carry-over bug).')
    say('ALL OK' if not bad_total else f'{bad_total} scenario(s) with problems')
    path = os.path.join(a.out, f'buffer_check_{datetime.datetime.now():%Y%m%d_%H%M%S}.txt')
    with open(path, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    print('report:', path)
    try:
        cam.close()
    except Exception:
        pass
    return 1 if bad_total else 0


if __name__ == '__main__':
    sys.exit(main())
