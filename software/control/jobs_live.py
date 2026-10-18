"""Live view of a running job: what the scope is doing now, countdown to the next acquisition, progress.

JobProgress  - the logic (no Qt): digests the messages sent by the running job (api.Scope observer) and the plan from
               the dry run, and answers "where are we".
LiveJobView  - the widget (JOBs tab): status line with countdown, progress bars, one box per time point and one box per
               position of the current time point. Needs no matplotlib. Updates five times per second.

Messages (dicts, see api.Scope._notify):
  {'type':'state', 'state':'moving'|'capturing'|'autofocus'|'waiting'|'paused'|'idle', 'point':idx, 'channel':name, 'tp':idx,
   'until':job time (s) for waits, 'reason':'interval'|'wait'|'wait_until', 'next_tp':idx, 't':job time}
  {'type':'timepoint', 'index', 'start', 'late', 'planned', 'n', 'interval', 'next_start'}
  {'type':'done', 'kind':'move'|'capture'|'autofocus'|'wait'|'timepoint', 'point', 'tp', 'channel', 't0', 't1'}
  {'type':'finished', 'status', 'error', 't'}
"""
import math
import time
import datetime


def fmt_s(seconds):
    """1h 02m 03s / 4m 05s / 12s"""
    s = max(0, int(round(seconds)))
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    if h:
        return f'{h} h {m:02d} min {s:02d} s'
    if m:
        return f'{m} min {s:02d} s'
    return f'{s} s'


def fmt_clock(epoch_s):
    return datetime.datetime.fromtimestamp(epoch_s).strftime('%a %H:%M:%S')


class JobProgress:
    """State of a running job. `clock` returns wall-clock seconds (injectable for tests)."""

    def __init__(self, clock=time.time):
        self._clock = clock
        self.reset()

    def reset(self, plan_events=None, plan_duration=None, points=None):
        self.points = {p.index: p for p in (points or [])}
        self.n_tp = None
        self.plan_tp_start, self.plan_visits, self.plan_images = {}, {}, {}
        self.plan_total_images = 0
        self.plan_duration = plan_duration
        for e in plan_events or []:
            tp = e.get('tp')
            if e['kind'] == 'timepoint':
                self.plan_tp_start[e['tp']] = e['t0']
            elif tp is not None and e['point'] is not None and e['kind'] in ('move', 'capture', 'autofocus'):
                v = self.plan_visits.setdefault(tp, [])
                if e['point'] not in v:
                    v.append(e['point'])
            if e['kind'] == 'capture':
                self.plan_total_images += 1
                if tp is not None:
                    self.plan_images[tp] = self.plan_images.get(tp, 0) + 1
        if self.plan_tp_start:
            self.n_tp = max(self.plan_tp_start) + 1
        # live state
        self.active = False
        self.status = None                  # None while running, then 'completed' | 'aborted' | 'failed' | 'invalid'
        self.error = ''
        self.state = dict(state='starting')
        self.tp_start, self.tp_late = {}, {}
        self.current_tp = None
        self.next_start = None
        self.interval = None
        self.tp_images, self.tp_points_done, self.tp_af = {}, {}, {}
        self.visits_seen = {}               # tp -> points visited so far in order (used if the plan has no entry)
        self.images_done = 0
        self.t_msg, self.wall_msg = 0.0, self._clock()
        self.finished_t = None

    def start(self, plan_events=None, plan_duration=None, points=None):
        self.reset(plan_events, plan_duration, points)
        self.active = True

    # ---------------------------------------------------------------------------------------
    def update(self, msg):
        self.t_msg, self.wall_msg = msg.get('t', self.t_msg), self._clock()
        typ = msg.get('type')
        if typ == 'state':
            self.state = dict(msg)
        elif typ == 'timepoint':
            i = msg['index']
            self.current_tp = i
            self.tp_start[i] = msg['start']
            self.tp_late[i] = msg.get('late', 0.0)
            self.next_start = msg.get('next_start')
            self.interval = msg.get('interval')
            if msg.get('n') is not None:
                self.n_tp = msg['n']
        elif typ == 'done':
            k, tp, pt = msg.get('kind'), msg.get('tp'), msg.get('point')
            if pt is not None and k in ('move', 'capture', 'autofocus'):
                seen = self.visits_seen.setdefault(tp, [])
                if pt not in seen:
                    seen.append(pt)
            if k == 'capture':
                self.images_done += 1
                self.tp_images[tp] = self.tp_images.get(tp, 0) + 1
                self.tp_points_done.setdefault(tp, set()).add(pt)
            elif k == 'autofocus':
                self.tp_af.setdefault(tp, set()).add(pt)
        elif typ == 'finished':
            self.status = msg.get('status')
            self.error = msg.get('error', '')
            self.finished_t = msg.get('t')
            self.active = False

    # ---------------------------------------------------------------------------------------
    def job_time(self):
        """Current job time (s since the job started), extrapolated from the last message."""
        if self.status is not None and self.finished_t is not None:
            return self.finished_t
        if self.state.get('state') == 'paused':
            return self.t_msg                   # job time stands still while paused
        return self.t_msg + (self._clock() - self.wall_msg) if self.active else self.t_msg

    def is_paused(self):
        return self.active and self.status is None and self.state.get('state') == 'paused'

    def countdown_text(self):
        """Big countdown while the job waits: 'Next acquisition in 125 s  (2 min 05 s)'. None if not waiting."""
        if not self.active or self.status is not None or self.state.get('state') != 'waiting':
            return None
        left = max(0.0, self.state.get('until', 0) - self.job_time())
        secs = int(math.ceil(left - 1e-9))
        what = 'Next acquisition in' if self.state.get('reason') == 'interval' else 'Waiting, ends in'
        return f'{what} {secs} s' + (f'   ({fmt_s(secs)})' if secs >= 120 else '')

    def _label(self, idx):
        p = self.points.get(idx)
        return f'#{idx}' + (f' ({p.id})' if p is not None and p.id else '')

    def _tp_text(self):
        if self.current_tp is None:
            return ''
        return f'time point {self.current_tp + 1}' + (f'/{self.n_tp}' if self.n_tp else '')

    def headline(self):
        """(text, countdown seconds or None)"""
        if self.status is not None:
            word = {'completed': 'Job completed', 'aborted': 'Job aborted', 'failed': 'Job FAILED', 'invalid': 'Job could not start'}.get(self.status, self.status)
            return word + (': ' + self.error.splitlines()[0] if self.error else ''), None
        if not self.active:
            return 'No job running', None
        st, now = self.state, self.job_time()
        s = st.get('state')
        if s == 'paused':
            return f'PAUSED for {fmt_s(self._clock() - self.wall_msg)} - adjust positions, then press "Resume job"   [{self._tp_text()}]', None
        if s == 'waiting':
            left = max(0.0, st.get('until', now) - now)
            if st.get('reason') == 'interval':
                nt = st.get('next_tp')
                label = f'time point {nt + 1}' + (f'/{self.n_tp}' if self.n_tp else '') if nt is not None else 'the next time point'
                return f'Waiting - {label} starts in {fmt_s(left)}  (at {fmt_clock(self._clock() + left)})', left
            return f'Waiting (scope.wait) - {fmt_s(left)} left  (until {fmt_clock(self._clock() + left)})', left
        where = self._tp_text()
        if s == 'moving':
            return f'Moving to position {self._label(st.get("point"))}   [{where}]', None
        if s == 'capturing':
            return f'Imaging position {self._label(st.get("point"))}, channel {st.get("channel")}   [{where}]', None
        if s == 'autofocus':
            return f'Autofocus at position {self._label(st.get("point"))}   [{where}]', None
        if s == 'idle':
            return f'Running   [{where}]', None
        return 'Job starting ...', None

    def details(self):
        """list of text lines"""
        lines = []
        now = self.job_time()
        lines.append(f'elapsed: {fmt_s(now)}')
        if self.current_tp is not None and self.current_tp in self.tp_start:
            run = now - self.tp_start[self.current_tp]
            txt = f'{self._tp_text()}: started {fmt_s(run)} ago'
            if self.tp_late.get(self.current_tp, 0) > 0.5:
                txt += f' (started {fmt_s(self.tp_late[self.current_tp])} late)'
            if self.next_start is not None and self.state.get('state') != 'waiting':
                rest = self.next_start - now
                txt += f'; next time point planned in {fmt_s(rest)}' if rest > 0 else '; the next time point will start immediately (this one takes longer than the interval)'
            lines.append(txt)
        total = self.plan_total_images
        lines.append(f'images: {self.images_done}' + (f' of {total} planned' if total else ''))
        late = sum(1 for v in self.tp_late.values() if v > 0.5)
        if late:
            lines.append(f'time points that started late: {late}')
        eta = self.estimated_end()
        if eta is not None:
            lines.append(f'estimated end: {fmt_clock(eta)} (in {fmt_s(eta - self._clock())})')
        return lines

    def estimated_end(self):
        """Wall-clock epoch of the estimated end: planned duration plus the delay already accumulated. None if unknown."""
        if self.status is not None or not self.active or not self.plan_duration or self.current_tp is None:
            return None
        planned = self.plan_tp_start.get(self.current_tp)
        if planned is None or self.current_tp not in self.tp_start:
            return None
        lag = max(0.0, self.tp_start[self.current_tp] - planned)
        remaining = self.plan_duration + lag - self.job_time()
        return self._clock() + max(0.0, remaining)

    def overall_fraction(self):
        if self.status == 'completed':
            return 1.0
        if self.plan_total_images:
            return min(1.0, self.images_done / self.plan_total_images)
        return None

    def tp_boxes(self, max_boxes=400):
        """[(index, state)] state: done | current | waiting | upcoming ; plus late flag via tp_late"""
        if not self.n_tp:
            return []
        out = []
        waiting_for = self.state.get('next_tp') if self.state.get('state') == 'waiting' and self.state.get('reason') == 'interval' else None
        for i in range(self.n_tp):
            if self.status == 'completed' or (self.current_tp is not None and i < self.current_tp) or (waiting_for is not None and i < waiting_for):
                st = 'done'
            elif i == self.current_tp:
                st = 'current'
            elif waiting_for == i:
                st = 'waiting'
            else:
                st = 'upcoming'
            out.append((i, st))
        return out

    def position_boxes(self):
        """[(point index, state)] for the current time point: done | current | pending ; state 'af' flag in set tp_af"""
        tp = self.current_tp
        if tp is None:
            return []
        order = list(self.plan_visits.get(tp, []))
        for p in self.visits_seen.get(tp, []):
            if p not in order:
                order.append(p)
        done = self.tp_points_done.get(tp, set())
        cur = self.state.get('point') if self.state.get('state') in ('moving', 'capturing', 'autofocus') else None
        out = []
        for p in order:
            if p == cur:
                out.append((p, 'current'))
            elif p in done:
                out.append((p, 'done'))
            else:
                out.append((p, 'pending'))
        return out


# ------------------------------------------------------------------------------------------------
# widget
# ------------------------------------------------------------------------------------------------
try:
    from qtpy.QtCore import Qt, QTimer, QRectF
    from qtpy.QtGui import QPainter, QColor, QPen, QBrush
    from qtpy.QtWidgets import QFrame, QVBoxLayout, QLabel, QProgressBar, QWidget, QSizePolicy
    _QT = True
except Exception:       # pure-logic use (tests) without Qt
    _QT = False

COLORS = dict(done='#55a868', current='#4c72b0', waiting='#f0b429', upcoming='#dddddd', pending='#dddddd', late='#dd8452', af='#c44e52')

if _QT:
    class _Strip(QWidget):
        """a row of small boxes; 'items' is a list of (key, state)"""

        def __init__(self, title, one_based=False, parent=None):
            super().__init__(parent)
            self.one_based = one_based
            self.title, self.items, self.marks, self.late = title, [], set(), set()
            self.setMinimumHeight(44)
            self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        def set_items(self, items, marks=(), late=()):
            if (items, set(marks), set(late)) != (self.items, self.marks, self.late):
                self.items, self.marks, self.late = list(items), set(marks), set(late)
                self.update()

        def paintEvent(self, ev):
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing, False)
            p.setPen(QColor('#555555'))
            p.drawText(2, 12, self.title)
            n = len(self.items)
            if not n:
                return
            top, h = 18, self.height() - 22
            width = max(1, self.width() - 4)
            gap = 1 if width / n > 6 else 0
            bw = max(1.0, width / n - gap)
            for k, (key, state) in enumerate(self.items):
                x = 2 + k * (bw + gap)
                p.fillRect(QRectF(x, top, bw, h), QColor(COLORS.get(state, '#dddddd')))
                if key in self.late:
                    p.fillRect(QRectF(x, top + h - 4, bw, 4), QColor(COLORS['late']))
                if key in self.marks and bw >= 4:
                    p.fillRect(QRectF(x + max(0.0, bw - 9), top + 2, min(7.0, bw - 1), 7), QColor(COLORS['af']))
            if n <= 60 and bw >= 14:
                p.setPen(QColor('white'))
                for k, (key, state) in enumerate(self.items):
                    if state in ('done', 'current'):
                        p.drawText(QRectF(2 + k * (bw + gap), top, bw, h), Qt.AlignCenter, str(key + 1 if self.one_based else key))
            p.end()

    class LiveJobView(QFrame):
        """Status of the running job; feed it with on_message() (connected to MultiPointController.signal_job_status)."""

        def __init__(self, parent=None):
            super().__init__(parent)
            self.model = JobProgress()
            self.label_head = QLabel('No job running')
            f = self.label_head.font(); f.setPointSize(f.pointSize() + 3); f.setBold(True)
            self.label_head.setFont(f)
            self.label_head.setWordWrap(True)
            self.bar_countdown = QProgressBar(); self.bar_countdown.setTextVisible(False); self.bar_countdown.setFixedHeight(8)
            self.bar_overall = QProgressBar(); self.bar_overall.setFormat('images %v / %m'); self.bar_overall.setFixedHeight(16)
            self.label_count = QLabel('')
            fc = self.label_count.font(); fc.setPointSize(fc.pointSize() + 12); fc.setBold(True)
            self.label_count.setFont(fc)
            self.label_count.setAlignment(Qt.AlignCenter)
            self.label_count.setVisible(False)
            self.label_details = QLabel('')
            self.label_details.setWordWrap(True)
            self.strip_tp = _Strip('time points  (green: done, blue: now, yellow: waiting to start, orange foot: started late)', one_based=True)
            self.strip_pos = _Strip('positions of the current time point  (green: imaged, blue: now, red dot: autofocus done)')
            lay = QVBoxLayout(self)
            lay.setContentsMargins(4, 4, 4, 4)
            for w in (self.label_head, self.label_count, self.bar_countdown, self.label_details, self.bar_overall, self.strip_tp, self.strip_pos):
                lay.addWidget(w)
            self.setFrameShape(QFrame.StyledPanel)
            self._countdown_total = None
            self.timer = QTimer(self)
            self.timer.setInterval(200)
            self.timer.timeout.connect(self.refresh)
            self.refresh()

        # --- API
        def begin(self, plan_events=None, plan_duration=None, points=None):
            self._countdown_total = None
            self.model.start(plan_events, plan_duration, points)
            self.timer.start()
            self.refresh()

        def on_message(self, msg):
            if isinstance(msg, dict):
                self.model.update(msg)
                if msg.get('type') == 'state' and msg.get('state') == 'waiting':
                    self._countdown_total = max(1e-6, msg.get('until', 0) - msg.get('t', 0))
                if msg.get('type') == 'finished':
                    self.timer.stop()
                self.refresh()

        def refresh(self):
            m = self.model
            text, countdown = m.headline()
            self.label_head.setText(text)
            ct = m.countdown_text()
            self.label_count.setText(ct or '')
            self.label_count.setVisible(ct is not None)
            self.label_head.setStyleSheet('color: #b35900' if m.is_paused() else '')
            if countdown is not None and self._countdown_total:
                self.bar_countdown.setRange(0, 1000)
                self.bar_countdown.setValue(int(1000 * (1 - min(1.0, countdown / self._countdown_total))))
                self.bar_countdown.setVisible(True)
            else:
                self.bar_countdown.setVisible(False)
            self.label_details.setText('\n'.join(m.details()) if (m.active or m.status) else '')
            frac = m.overall_fraction()
            if frac is not None and m.plan_total_images:
                self.bar_overall.setRange(0, m.plan_total_images)
                self.bar_overall.setValue(min(m.images_done, m.plan_total_images))
                self.bar_overall.setVisible(True)
            else:
                self.bar_overall.setVisible(False)
            tps = m.tp_boxes()
            self.strip_tp.set_items(tps, late=[i for i, v in m.tp_late.items() if v > 0.5])
            self.strip_tp.setVisible(bool(tps))
            pos = m.position_boxes()
            tp = m.current_tp
            self.strip_pos.set_items(pos, marks=m.tp_af.get(tp, set()))
            self.strip_pos.setVisible(bool(pos))
