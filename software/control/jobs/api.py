"""User-level scripting API for JOBs: the `scope` object a job script receives.

A job is a plain Python file that defines  run_job(scope).  It describes WHAT is done and WHEN
(loops over points and time points, channels, autofocus, waiting). It never talks to hardware:
everything goes through the high-level methods of `scope`; the hardware is driven by a backend
(control/jobs/hardware_backend.py) that reuses the tested movement/imaging/autofocus code.

This module has no hardware and no Qt dependency, so jobs can be checked (dry run) and tested anywhere.
"""
import os
import re
import sys
import csv
import time
import datetime

import numpy as np

AF_MAX_Z_DRIFT_MM = 0.5     # as in the standard acquisition: a larger focus shift is not stored for later visits


class JobError(Exception):
    """The job is wrong (unknown channel, bad argument, no run_job, ...) or could not be executed."""


class JobAborted(BaseException):
    """Raised inside the job when the user aborts. Derives from BaseException so that a job's own
    `except Exception:` does not swallow it."""


# ------------------------------------------------------------------------------------------------
# clocks (real and simulated, the latter for dry runs and tests)
# ------------------------------------------------------------------------------------------------
class RealClock:
    def __init__(self):
        self._t0 = time.monotonic()

    def now(self):
        return time.monotonic() - self._t0

    def sleep(self, seconds):
        if seconds > 0:
            time.sleep(seconds)

    def wait_until(self, t_abs, should_abort):
        """Sleep until now() >= t_abs in short steps; returns False if aborted."""
        while True:
            if should_abort():
                return False
            remaining = t_abs - self.now()
            if remaining <= 0:
                return True
            time.sleep(min(remaining, 0.1))


class SimClock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += max(0.0, seconds)

    def wait_until(self, t_abs, should_abort):
        if should_abort():
            return False
        self.t = max(self.t, t_abs)
        return True


# ------------------------------------------------------------------------------------------------
# data classes
# ------------------------------------------------------------------------------------------------
class Point:
    """One imaging position. x, y, z in mm (stage coordinates). z is updated after an autofocus."""

    def __init__(self, id, group, index, number, x, y, z):
        self.id = id              # the ID typed in the positions table, e.g. 'wellA-3' ('' if none)
        self.group = group        # part of the ID before the first '-'  ('default' if the ID is empty)
        self.index = index        # 0-based position in the table (all points)
        self.number = number      # 0-based position within its group
        self.x, self.y, self.z = x, y, z
        self.z_initial = z
        self.table_xyz = (x, y, z)   # as read from the positions table (used to detect edits made while the job is paused)

    def __repr__(self):
        return f'Point(id={self.id!r}, group={self.group!r}, index={self.index}, x={self.x:.3f}, y={self.y:.3f}, z={self.z})'


class TimePoint:
    def __init__(self, index, start, late):
        self.index = index        # 0-based
        self.start = start        # seconds since the job started
        self.late = late          # seconds this time point started later than planned (0 = on time)

    def __repr__(self):
        return f'TimePoint(index={self.index}, start={self.start:.1f}s, late={self.late:.1f}s)'


def group_of(point_id):
    g = str(point_id).split('-')[0].strip()
    return g if g else 'default'


def make_points(ids, coords):
    """ids: list of str; coords: list of (x, y[, z]) in mm -> list of Point (GUI order)."""
    pts, per_group = [], {}
    for i, (pid, c) in enumerate(zip(ids, coords)):
        pid = '' if pid is None else str(pid)
        g = group_of(pid)
        n = per_group.get(g, 0)
        per_group[g] = n + 1
        z = float(c[2]) if len(c) > 2 else None
        pts.append(Point(pid, g, i, n, float(c[0]), float(c[1]), z))
    return pts


def resolve_channel_name(name, channel_names):
    """The channel a (part of a) name stands for: exact match, else the one channel that contains it (case-insensitive).
    Raises JobError for unknown or ambiguous names (used by Scope and by the job designer's checker)."""
    if not isinstance(name, str):
        raise JobError(f'channel must be a name (text), got {name!r}')
    if name in channel_names:
        return name
    low = name.lower()
    hits = [c for c in channel_names if low in c.lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise JobError(f'unknown channel {name!r}. Available: {list(channel_names)}')
    raise JobError(f'channel {name!r} is ambiguous: {hits}. Use a longer part of the name.')


def _safe(s):
    return re.sub(r'[^A-Za-z0-9._+-]+', '_', str(s)).strip('_') or 'x'


# ------------------------------------------------------------------------------------------------
# backend interface (implemented by the hardware backend and by the dry-run backend)
# ------------------------------------------------------------------------------------------------
class Backend:
    def channels(self):                 # list of channel names
        raise NotImplementedError

    def channel_info(self, name):       # dict(exposure_ms, gain, intensity)
        return {}

    def move_to(self, x, y, z):         # absolute stage move in mm, returns when settled
        raise NotImplementedError

    def position(self):                 # (x, y, z) mm
        raise NotImplementedError

    def capture(self, channel):         # -> (image ndarray, info dict)
        raise NotImplementedError

    def autofocus(self, channel=None, focus_measure=None):  # -> z in mm after focusing
        raise NotImplementedError

    def save_image(self, image, path, channel):
        raise NotImplementedError

    def abort_requested(self):
        return False

    # --- pause / resume (optional; a backend that never pauses needs none of these)
    def pause_requested(self):          # True while the user wants the job paused
        return False

    def take_new_positions(self):       # -> list of (x, y, z) in table order (same length as the job's points) or None
        return None

    def on_pause(self):                 # called when the job has reached a safe point and is paused
        pass

    def on_resume(self):                # called just before the job continues
        pass

    def log(self, message):
        pass

    def cleanup(self):
        pass


# ------------------------------------------------------------------------------------------------
# the scope
# ------------------------------------------------------------------------------------------------
class Scope:
    """What a job script sees. See control/jobs/README.md for the full description."""

    def __init__(self, backend, points, experiment_dir=None, clock=None, save_images=True, max_events=None, record=False, observer=None, trace_file=None):
        self._backend = backend
        self._points = list(points)
        self._dir = experiment_dir
        self._clock = clock or RealClock()
        self._save = save_images and experiment_dir is not None
        self._max_events = max_events
        self._events = 0
        self.events = [] if record else None     # plan of the job for the graphical preview (dry run)
        self._observer = observer                # live view: called with small dicts (see _notify); must never break the job
        self._trace_file = os.path.abspath(trace_file) if trace_file else None   # job file: recorded events get the line number of the call (job designer)
        self._trace_cache = {}
        self._t0 = self._clock.now()
        self._tp = None
        self._current_point = None
        self._paused_total = 0.0                 # seconds spent paused (job time does not count them)
        self._used_paths = set()
        self._image_counter = 0
        self._csv = {}
        self._log_file = None
        self._channel_names = list(backend.channels())
        self.stats = dict(images=0, autofocus=0, moves=0, timepoints=0, late_timepoints=0, max_late_s=0.0,
                          per_channel={}, per_group={}, exposures={}, warnings=[], paused_s=0.0)
        self._groups = {}
        for p in self._points:
            self._groups.setdefault(p.group, []).append(p)

    # --- information -------------------------------------------------------------------------
    @property
    def channels(self):
        """Names of the available channels (those of the channel configuration)."""
        return list(self._channel_names)

    @property
    def groups(self):
        """dict: group name -> list of Points (GUI order)."""
        return {g: list(p) for g, p in self._groups.items()}

    @property
    def all_points(self):
        return list(self._points)

    def points(self, *groups):
        """Points in GUI order. points() = all; points('A') or points('A', 'B') = those groups, in the GUI order."""
        if not groups:
            return list(self._points)
        names = []
        for g in groups:
            names.extend(g if isinstance(g, (list, tuple)) else [g])
        unknown = [g for g in names if g not in self._groups]
        if unknown:
            raise JobError(f'unknown group {unknown}; groups are: {list(self._groups)}')
        return [p for p in self._points if p.group in names]

    @property
    def elapsed(self):
        """Seconds since the job started."""
        return self._clock.now() - self._t0

    @property
    def experiment_dir(self):
        return self._dir

    @property
    def abort_requested(self):
        return bool(self._backend.abort_requested())

    def check_abort(self):
        if self._backend.abort_requested():
            raise JobAborted()

    # --- logging -----------------------------------------------------------------------------
    def log(self, message):
        line = f'[{self.elapsed:9.1f} s] {message}'
        self._backend.log(line)
        if self._dir is not None:
            if self._log_file is None:
                self._log_file = open(os.path.join(self._dir, 'job_log.txt'), 'a', encoding='utf-8')
            self._log_file.write(line + '\n')
            self._log_file.flush()

    def warn(self, message):
        self.stats['warnings'].append(message)
        self.log('WARNING: ' + message)

    def _notify(self, msg):
        """Tell the live view (if any) what is going on. Messages are plain dicts with 'type' and 't' (job time, s)."""
        if self._observer is not None:
            msg.setdefault('t', self.elapsed)
            try:
                self._observer(msg)
            except Exception:
                pass

    def _state(self, state, **kw):
        if self._observer is not None:
            self._notify(dict(type='state', state=state, tp=self._tp.index if self._tp is not None else None, **kw))

    def _rec(self, kind, t0, t1=None, **kw):
        if self.events is not None or self._observer is not None:
            pt = kw.pop('point', None)
            ev = dict(kind=kind, t0=t0, t1=self.elapsed if t1 is None else t1,
                      point=pt.index if pt is not None else None,
                      tp=self._tp.index if self._tp is not None else None, **kw)
            if self._trace_file is not None:
                line = self._job_line()
                if line is not None:
                    ev['line'] = line
            if self.events is not None:
                self.events.append(ev)
            self._notify(dict(type='done', **ev))

    def _job_line(self):
        """Line in the job file that is executing (innermost frame of the job file), None if not found."""
        f = sys._getframe(1)
        while f is not None:
            fn = f.f_code.co_filename
            hit = self._trace_cache.get(fn)
            if hit is None:
                hit = self._trace_cache[fn] = (os.path.abspath(fn) == self._trace_file)
            if hit:
                return f.f_lineno
            f = f.f_back
        return None

    def _tick(self, restore=True):
        self.check_abort()
        self._events += 1
        if self._max_events and self._events > self._max_events:
            raise JobError(f'the job did not finish after {self._max_events} operations (endless loop?)')
        if self._backend.pause_requested():
            self._pause_now(restore)

    # --- pause / resume ----------------------------------------------------------------------
    def _pause_now(self, restore=True):
        """Hold the job here (between two operations) until the backend says resume. Job time does not run while
        paused, so planned waits/intervals simply continue where they were. On resume the positions edited in the table
        meanwhile are taken over, and the stage returns to the current point."""
        t_p = self._clock.now()
        pt = self._current_point
        self._state('paused', point=pt.index if pt is not None else None, t=self.elapsed)
        self.log('job PAUSED')
        self._backend.on_pause()
        while self._backend.pause_requested():
            if self._backend.abort_requested():
                raise JobAborted()
            self._clock.sleep(0.1)
        self._t0 += self._clock.now() - t_p           # job time did not run while paused
        self._apply_positions(self._backend.take_new_positions())
        self._backend.on_resume()
        self._paused_total += self._clock.now() - t_p
        self.stats['paused_s'] = round(self._paused_total, 1)
        self.log(f'job RESUMED (paused for {self._clock.now() - t_p:.1f} s)')
        self._state('idle', reason='resumed')
        pt = self._current_point
        if restore and pt is not None:
            self._state('moving', point=pt.index)
            self._backend.move_to(pt.x, pt.y, pt.z)

    def _apply_positions(self, new):
        if new is None:
            return
        if len(new) != len(self._points):
            self.warn(f'the positions table has {len(new)} entries but the job started with {len(self._points)}; '
                      f'positions added or removed while paused are ignored')
            return
        changed = 0
        for p, c in zip(self._points, new):
            xyz = (float(c[0]), float(c[1]), float(c[2]) if len(c) > 2 and c[2] is not None else None)
            if xyz != p.table_xyz:
                p.x, p.y, p.z = xyz
                p.z_initial = xyz[2]
                p.table_xyz = xyz
                changed += 1
        if changed:
            self.log(f'{changed} position(s) were changed while the job was paused; the new coordinates are used from now on')

    def _sleep_until(self, target, **state_kw):
        """Wait until job time `target` (s); abort and pause stay responsive. Returns when the time has come."""
        while True:
            self.check_abort()
            if self._backend.pause_requested():
                self._pause_now(True)
                self._state('waiting', until=target, **state_kw)     # the countdown continues with the same target
                continue
            if self.elapsed >= target:
                return
            stop = lambda: bool(self._backend.abort_requested() or self._backend.pause_requested())
            if self._clock.wait_until(self._t0 + target, stop):
                return

    # --- channels ----------------------------------------------------------------------------
    def _resolve_channel(self, name):
        return resolve_channel_name(name, self._channel_names)

    # --- time --------------------------------------------------------------------------------
    def wait(self, seconds):
        """Wait (the abort button still works)."""
        self._tick()
        if seconds < 0:
            raise JobError('wait(seconds): seconds must be >= 0')
        t0 = self.elapsed
        self._state('waiting', until=t0 + seconds, reason='wait')
        self._sleep_until(t0 + seconds, reason='wait')
        self._rec('wait', t0)

    def wait_until(self, elapsed_seconds):
        """Wait until `elapsed_seconds` have passed since the start of the job (returns at once if already later)."""
        self._tick()
        t0 = self.elapsed
        self._state('waiting', until=max(t0, elapsed_seconds), reason='wait_until')
        self._sleep_until(elapsed_seconds, reason='wait_until')
        self._rec('wait', t0)

    def timepoints(self, n, interval=None):
        """Loop over n time points:  for t in scope.timepoints(48, interval=1800): ...

        interval (s) is the time from the START of one time point to the START of the next. If a time point
        takes longer than the interval, the next one starts immediately (none is skipped), the delay is
        logged and the following interval counts from that start. n=None runs until aborted."""
        i, prev_start, planned = 0, None, None
        while n is None or i < n:
            self._tick()
            if i > 0 and interval:
                planned = prev_start + interval
                t_w0 = self.elapsed
                self._state('waiting', until=planned, reason='interval', next_tp=i)
                self._sleep_until(planned, reason='interval', next_tp=i)
                self._rec('wait', t_w0)
            start = self.elapsed
            late = max(0.0, start - planned) if planned is not None else 0.0
            tp = TimePoint(i, start, late)
            self._tp = tp
            self._rec('timepoint', start, start, late=late, planned=planned)
            self._notify(dict(type='timepoint', index=i, start=start, late=late, planned=planned, n=n, interval=interval,
                              next_start=(start + interval) if interval else None))
            self.stats['timepoints'] += 1
            if late > 0.5:
                self.stats['late_timepoints'] += 1
                self.stats['max_late_s'] = max(self.stats['max_late_s'], late)
                self.log(f'time point {i} started {late:.1f} s late (the previous one took longer than the interval)')
            else:
                self.log(f'time point {i} starts')
            yield tp
            prev_start = start
            i += 1

    # --- stage -------------------------------------------------------------------------------
    def move_to(self, point):
        """Move the stage to a Point (x, y, z; with all the usual backlash/safety handling)."""
        self._tick(restore=False)
        if isinstance(point, int) and not isinstance(point, bool):
            point = self._points[point]
        if not isinstance(point, Point):
            raise JobError(f'move_to needs a Point from scope.points(), got {point!r}')
        t0 = self.elapsed
        self._state('moving', point=point.index)
        self._backend.move_to(point.x, point.y, point.z)
        self._rec('move', t0, point=point)
        self._current_point = point
        self.stats['moves'] += 1

    # --- autofocus ---------------------------------------------------------------------------
    def autofocus(self, channel=None, focus_measure=None):
        """Contrast autofocus at the current position, with the settings of the Autofocus tab
        (focus measure, number of planes, step). channel: autofocus channel, default = the one of the Autofocus tab.
        focus_measure: e.g. "GLVA" or "LAPE", for this autofocus only (default = the one of the Autofocus tab, which stays unchanged).
        The focused z is remembered for the current point, so later visits start there. Returns z in mm."""
        self._tick()
        ch = self._resolve_channel(channel) if channel else None
        z_before = self._backend.position()[2]
        t_start = self.elapsed
        self._state('autofocus', point=self._current_point.index if self._current_point else None)
        z_after = self._backend.autofocus(ch) if focus_measure is None else self._backend.autofocus(ch, focus_measure=focus_measure)
        pt = self._current_point
        if pt is not None and pt.z_initial is not None and abs(z_after - pt.z_initial) > AF_MAX_Z_DRIFT_MM:
            self.warn(f'autofocus moved z by {z_after - pt.z_initial:.3f} mm at point {pt.index} ({pt.id}); '
                      f'more than {AF_MAX_Z_DRIFT_MM} mm - the new z is not stored for later visits')
        elif pt is not None:
            pt.z = z_after
        self.stats['autofocus'] += 1
        self._rec('autofocus', t_start, point=pt, channel=ch)
        self._write_row('autofocus', dict(
            time_s=round(t_start, 2), point_index=pt.index if pt else '', point_id=pt.id if pt else '',
            timepoint=self._tp.index if self._tp else '', channel=ch or '', z_before_mm=z_before, z_after_mm=z_after,
            duration_s=round(self.elapsed - t_start, 2)))
        return z_after

    # --- imaging -----------------------------------------------------------------------------
    def capture(self, channel, save=True):
        """Take one image in `channel` at the current position and return it as a NumPy array.
        The image is saved automatically (unique file name) and listed in images.csv; save=False returns it only."""
        self._tick()
        ch = self._resolve_channel(channel)
        t_cap = self.elapsed
        self._state('capturing', point=self._current_point.index if self._current_point else None, channel=ch)
        image, info = self._backend.capture(ch)
        info = dict(info or {})
        pt, tp = self._current_point, self._tp
        self._image_counter += 1
        rel = ''
        if save and self._save:
            folder = os.path.join(self._dir, f't{tp.index:04d}' if tp else 'no_timepoint')
            os.makedirs(folder, exist_ok=True)
            key = f'p{pt.index:04d}_{_safe(pt.id)}' if (pt is not None and pt.id) else (f'p{pt.index:04d}' if pt is not None else 'stage')
            base = f'{key}_{_safe(ch)}'
            path, n = os.path.join(folder, base + '.tiff'), 1
            while path in self._used_paths or os.path.exists(path):
                n += 1
                path = os.path.join(folder, f'{base}_{n}.tiff')
            self._used_paths.add(path)
            self._backend.save_image(image, path, ch)
            rel = os.path.relpath(path, self._dir)
        self._rec('capture', t_cap, point=pt, channel=ch)
        self.stats['images'] += 1
        self.stats['per_channel'][ch] = self.stats['per_channel'].get(ch, 0) + 1
        if pt is not None:
            self.stats['per_group'][pt.group] = self.stats['per_group'].get(pt.group, 0) + 1
        if info.get('exposure_ms') is not None:
            self.stats['exposures'][ch] = info['exposure_ms']
        self._write_row('images', dict(
            image_id=self._image_counter, file=rel, time_s=round(t_cap, 2), wall_time=datetime.datetime.now().isoformat(timespec='milliseconds'),
            timepoint=tp.index if tp else '', point_index=pt.index if pt else '', point_id=pt.id if pt else '',
            group=pt.group if pt else '', channel=ch, x_mm=info.get('x_mm', ''), y_mm=info.get('y_mm', ''), z_mm=info.get('z_mm', ''),
            exposure_ms=info.get('exposure_ms', ''), gain=info.get('gain', ''), intensity=info.get('intensity', '')))
        return image

    def capture_channels(self, channels, save=True):
        """capture() for several channels in the given order; returns {channel: image}."""
        return {self._resolve_channel(c): self.capture(c, save=save) for c in channels}

    # --- bookkeeping -------------------------------------------------------------------------
    def _write_row(self, table, row):
        if self._dir is None:
            return
        entry = self._csv.get(table)
        if entry is None:
            f = open(os.path.join(self._dir, table + '.csv'), 'w', newline='', encoding='utf-8')
            w = csv.DictWriter(f, fieldnames=list(row))
            w.writeheader()
            entry = self._csv[table] = (f, w)
        entry[1].writerow(row)
        entry[0].flush()

    def _close(self):
        for f, _ in self._csv.values():
            f.close()
        self._csv = {}
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None
