"""Graphic views of a job plan for the Job designer. Native Qt painting only: no matplotlib needed, all views are interactive.

  PlanView       timeline per position: one coloured tick per image (colour = channel), autofocus, moves, time points.
                 Mouse wheel = zoom time, drag = pan, double click = whole job, hover = details, click = jump to the line in the code.
  StageMapView   where the positions are (colour = group), the order in the first time point, positions the job never images.
  BudgetView     working time per time point (moving / imaging / autofocus) against the interval: shows if the interval is too short.
  MatrixView     number of images per channel (and autofocus runs) for each time point: shows patterns such as "BF every 4th time point".

All views take a recorded dry-run result (control.jobs.runner.JobResult with events) and the list of Points.
"""
import bisect
import math

import numpy as np
from qtpy.QtCore import Qt, Signal, QPointF, QRectF, QSize
from qtpy.QtGui import QPainter, QColor, QPen, QBrush, QFont, QFontMetrics, QPolygonF, QPalette
from qtpy.QtWidgets import QWidget, QToolTip, QSizePolicy

from control.jobs.plot import CHANNEL_COLORS, GROUP_COLORS

STEPS = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 21600, 43200, 86400, 172800, 432000]
KIND_MOVE, KIND_CAPTURE, KIND_AF = 0, 1, 2
BUDGET_COLORS = dict(move='#a9a9a9', capture='#4c72b0', autofocus='#c44e52')


def fmt_t(t):
    """H:MM:SS(.s) for a time in seconds"""
    t = max(0.0, t)
    h, r = divmod(t, 3600)
    m, s = divmod(r, 60)
    return f'{int(h)}:{int(m):02d}:{s:04.1f}'


def fmt_dur(s):
    if s >= 7200:
        return f'{s / 3600:.1f} h'
    if s >= 120:
        return f'{s / 60:.1f} min'
    return f'{s:.1f} s'


def nice_step(span, target=8):
    want = span / max(1, target)
    for s in STEPS:
        if s >= want:
            return s
    return STEPS[-1]


def axis_unit(span):
    if span >= 2 * 3600:
        return 3600.0, 'h'
    if span >= 120:
        return 60.0, 'min'
    return 1.0, 's'


def color_maps(points, events, channel_order=None):
    names = list(channel_order or [])
    for e in events:
        if e.get('channel') and e['channel'] not in names:
            names.append(e['channel'])
    used = []
    for e in events:
        if e['kind'] == 'capture' and e['channel'] not in used:
            used.append(e['channel'])
    ch = {c: QColor(CHANNEL_COLORS[i % len(CHANNEL_COLORS)]) for i, c in enumerate(names)}
    groups = []
    for p in points:
        if p.group not in groups:
            groups.append(p.group)
    gr = {g: QColor(GROUP_COLORS[i % len(GROUP_COLORS)]) for i, g in enumerate(groups)}
    return ch, used, gr


class _View(QWidget):
    """base: palette colours, an optional message instead of the drawing"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.message = 'Press Test (F5) to see the plan.'
        self.message_error = False
        self.result = None
        self.points = []
        self.events = []

    # --- colours that work in light and dark themes
    def c_text(self):
        return self.palette().color(QPalette.WindowText)

    def c_muted(self):
        c = QColor(self.c_text())
        c.setAlpha(120)
        return c

    def c_grid(self):
        c = QColor(self.c_text())
        c.setAlpha(40)
        return c

    def c_bg(self):
        return self.palette().color(QPalette.Base)

    def small_font(self, px=11):
        f = QFont(self.font())
        f.setPixelSize(px)
        return f

    def clear(self, message='Press Test (F5) to see the plan.', error=False):
        self.result, self.points, self.events = None, [], []
        self.message, self.message_error = message, error
        self._data_changed()
        self.update()

    def set_result(self, result, points, channel_order=None):
        self.result, self.points = result, list(points)
        self.events = result.events or []
        self.channel_order = list(channel_order or [])
        self.message = '' if self.events else ('The job did nothing (no images, no moves).' if result.status == 'completed' else (result.error or result.status).splitlines()[0])
        self.message_error = result.status not in ('completed',)
        self._data_changed()
        self.update()

    def _data_changed(self):
        pass

    def _paint_message(self, p):
        p.setPen(QColor('#c0392b') if self.message_error else self.c_muted())
        p.setFont(self.small_font(12))
        p.drawText(self.rect().adjusted(12, 12, -12, -12), Qt.AlignCenter | Qt.TextWordWrap, self.message)

    def _title(self, p, text, x=6, y=14):
        p.setPen(self.c_text())
        f = self.small_font(11)
        f.setBold(True)
        p.setFont(f)
        p.drawText(x, y, text)


# ------------------------------------------------------------------------------------------------
class PlanView(_View):
    eventClicked = Signal(int)          # line number in the job file
    pointHovered = Signal(int)          # index of the hovered point, -1 = none
    viewChanged = Signal(float, float)

    LEFT, TOP, BOTTOM, RIGHT = 120, 44, 8, 14

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(300, 170)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.v0, self.v1 = 0.0, 1.0
        self.t_end = 1.0
        self._hover = -1                 # index into the arrays
        self._highlight_row = -1
        self._press = None
        self._moved = False
        self._empty_arrays()
        self.channel_order = []
        self.ch_colors, self.used, self.grp_colors = {}, [], {}

    # --- data
    def _empty_arrays(self):
        self.order, self.row_of = [], {}
        self.k = np.zeros(0, dtype=np.int8)
        self.t0 = self.t1 = np.zeros(0)
        self.row = np.zeros(0, dtype=np.int32)
        self.ch = np.zeros(0, dtype=np.int32)
        self.line = np.zeros(0, dtype=np.int32)
        self.tp = np.zeros(0, dtype=np.int32)
        self.ev_index = np.zeros(0, dtype=np.int32)
        self.tp_t, self.tp_late, self.tp_idx = np.zeros(0), np.zeros(0), np.zeros(0, dtype=np.int32)
        self.wait_t0 = self.wait_t1 = np.zeros(0)
        self.ch_names = []

    def _data_changed(self):
        self._empty_arrays()
        self.ch_colors, self.used, self.grp_colors = {}, [], {}
        self._hover = -1
        if not self.events:
            self.updateGeometry()
            return
        self.ch_colors, self.used, self.grp_colors = color_maps(self.points, self.events, getattr(self, 'channel_order', None))
        groups = list(self.grp_colors)
        self.order = sorted(self.points, key=lambda p: (groups.index(p.group), p.index))
        self.row_of = {p.index: i for i, p in enumerate(self.order)}
        self.ch_names = list(self.ch_colors)
        ks, t0, t1, rows, chs, lines, tps, idx = [], [], [], [], [], [], [], []
        tp_t, tp_late, tp_idx, w0, w1 = [], [], [], [], []
        for i, e in enumerate(self.events):
            kind = e['kind']
            if kind in ('capture', 'autofocus', 'move') and e.get('point') in self.row_of:
                ks.append({'move': KIND_MOVE, 'capture': KIND_CAPTURE, 'autofocus': KIND_AF}[kind])
                t0.append(e['t0']); t1.append(e['t1']); rows.append(self.row_of[e['point']])
                chs.append(self.ch_names.index(e['channel']) if e.get('channel') in self.ch_names else -1)
                lines.append(e.get('line') or -1)
                tps.append(e['tp'] if e.get('tp') is not None else -1)
                idx.append(i)
            elif kind == 'timepoint':
                tp_t.append(e['t0']); tp_late.append(e.get('late') or 0.0); tp_idx.append(e['tp'] if e.get('tp') is not None else len(tp_idx))
            elif kind == 'wait':
                w0.append(e['t0']); w1.append(e['t1'])
        self.k = np.array(ks, dtype=np.int8); self.t0 = np.array(t0); self.t1 = np.array(t1)
        self.row = np.array(rows, dtype=np.int32); self.ch = np.array(chs, dtype=np.int32)
        self.line = np.array(lines, dtype=np.int32); self.tp = np.array(tps, dtype=np.int32); self.ev_index = np.array(idx, dtype=np.int32)
        self.tp_t, self.tp_late, self.tp_idx = np.array(tp_t), np.array(tp_late), np.array(tp_idx, dtype=np.int32)
        self.wait_t0, self.wait_t1 = np.array(w0), np.array(w1)
        self.t_end = max([e['t1'] for e in self.events] + [1e-6])
        self.default_view()
        self.setMinimumHeight(self.TOP + self.BOTTOM + 7 * max(1, len(self.order)))
        self.updateGeometry()

    # --- view
    def fit(self):
        self.set_view(0.0, self.t_end * 1.002 + 1e-6)

    def zoom_to_timepoint(self, index, count=1):
        """show `count` time points from the start of `index` to the start of the one after them (or to the end of the last operation)"""
        sel = np.where(self.tp_idx == index)[0]
        if not len(sel):
            return False
        start = float(self.tp_t[sel[0]])
        last = index + count - 1
        ops = (self.tp >= index) & (self.tp <= last)
        end = float(self.t1[ops].max()) if ops.any() else start + 1.0
        nxt = self.tp_t[self.tp_idx == last + 1]
        if len(nxt) and float(nxt[0]) > end:
            end = float(nxt[0])
        pad = 0.012 * (end - start + 1)
        self.set_view(start - pad, end + pad)
        return True

    def default_view(self):
        """whole job when it is short, otherwise the first few time points (the whole job is one click away)"""
        n = len(self.tp_t)
        if n > 10:
            self.zoom_to_timepoint(0, count=3)
        else:
            self.fit()

    def set_view(self, v0, v1):
        span = max(0.2, v1 - v0)
        self.v0, self.v1 = v0, v0 + span
        self.viewChanged.emit(self.v0, self.v1)
        self.update()

    def highlight_point(self, index):
        r = self.row_of.get(index, -1)
        if r != self._highlight_row:
            self._highlight_row = r
            self.update()

    # --- geometry
    def _row_h(self):
        n = max(1, len(self.order))
        return max(6.0, min(26.0, (self.height() - self.TOP - self.BOTTOM) / n))

    def _plot_rect(self):
        n = max(1, len(self.order))
        return QRectF(self.LEFT, self.TOP, max(10, self.width() - self.LEFT - self.RIGHT), max(10.0, n * self._row_h()))

    def _x(self, t, r):
        return r.left() + (t - self.v0) / (self.v1 - self.v0) * r.width()

    def _t(self, x, r):
        return self.v0 + (x - r.left()) / r.width() * (self.v1 - self.v0)

    # --- painting
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        p.fillRect(self.rect(), self.c_bg())
        if not self.events:
            self._paint_message(p)
            return
        r = self._plot_rect()
        rh = self._row_h()
        n = len(self.order)
        span = self.v1 - self.v0
        unit, ulabel = axis_unit(span)
        # group bands + labels
        p.setFont(self.small_font(int(max(8, min(11, rh - 2)))))
        fm = QFontMetrics(p.font())
        ids = [q.id for q in self.order]
        unique = len(set(ids)) == len(ids) and all(ids)
        label_step = max(1, int(math.ceil(fm.height() / rh)))
        p.setClipRect(QRectF(0, r.top() - 1, self.width(), r.height() + 2))
        for i, q in enumerate(self.order):
            y = r.top() + i * rh
            col = QColor(self.grp_colors[q.group])
            band = QColor(col)
            band.setAlpha(26 if i % 2 == 0 else 8)
            p.fillRect(QRectF(r.left(), y, r.width(), rh), band)
            p.fillRect(QRectF(self.LEFT - 6, y, 4, rh), col)
            if i == self._highlight_row:
                hl = QColor('#f1c40f'); hl.setAlpha(80)
                p.fillRect(QRectF(0, y, self.width(), rh), hl)
            if i % label_step == 0:
                p.setPen(self.c_text())
                txt = q.id if unique else f'#{q.index} {q.id}'.strip()
                p.drawText(QRectF(4, y, self.LEFT - 12, rh), Qt.AlignVCenter | Qt.AlignRight, fm.elidedText(txt, Qt.ElideLeft, self.LEFT - 14))
            if i and self.order[i - 1].group != q.group:
                p.setPen(QPen(self.c_grid(), 1))
                p.drawLine(QPointF(self.LEFT - 6, y), QPointF(r.right(), y))
        p.setClipRect(r.adjusted(-1, -1, 1, 1))
        # waits (shaded over all rows)
        if len(self.wait_t0):
            m = (self.wait_t1 >= self.v0) & (self.wait_t0 <= self.v1)
            shade = QColor(self.c_text()); shade.setAlpha(14)
            for a, b in zip(self.wait_t0[m], self.wait_t1[m]):
                x0, x1 = self._x(a, r), self._x(b, r)
                if x1 - x0 >= 1:
                    p.fillRect(QRectF(x0, r.top(), x1 - x0, r.height()), shade)
        # grid + axis
        step = nice_step(span)
        first = math.ceil(self.v0 / step) * step
        p.setFont(self.small_font(10))
        t = first
        guard = 0
        while t <= self.v1 and guard < 400:
            x = self._x(t, r)
            p.setPen(QPen(self.c_grid(), 1))
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            guard += 1
            t += step
        # time point lines
        n_tp = len(self.tp_t)
        px_per_tp = r.width() / max(1.0, n_tp * span / max(self.t_end, 1e-9)) if n_tp else 1e9
        for tt, late in zip(self.tp_t, self.tp_late):
            if self.v0 <= tt <= self.v1:
                x = self._x(tt, r)
                if late > 0.5:
                    p.setPen(QPen(QColor('#d62728'), 1.4))
                else:
                    p.setPen(QPen(self.c_muted(), 1, Qt.DashLine if px_per_tp > 6 else Qt.SolidLine))
                p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
        # events
        if len(self.t0):
            vis = (self.t1 >= self.v0 - 1e-9) & (self.t0 <= self.v1)
            sel = np.where(vis)[0]
            kk, rr = self.k[sel], self.row[sel]
            px0 = np.floor((self.t0[sel] - self.v0) / span * r.width()).astype(np.int64)
            wpx = (self.t1[sel] - self.t0[sel]) / span * r.width()
            # moves
            mv = sel[kk == KIND_MOVE]
            if len(mv):
                col = QColor('#9a9a9a'); col.setAlpha(150)
                p.setPen(Qt.NoPen); p.setBrush(col)
                for i in self._thin(mv, r, with_channel=False):
                    x0, x1 = self._x(self.t0[i], r), self._x(self.t1[i], r)
                    y = r.top() + self.row[i] * rh
                    p.drawRect(QRectF(x0, y + rh * 0.42, max(1.0, x1 - x0), max(1.5, rh * 0.16)))
            # autofocus
            af = sel[kk == KIND_AF]
            for i in self._thin(af, r, with_channel=False):
                x0, x1 = self._x(self.t0[i], r), self._x(self.t1[i], r)
                y = r.top() + self.row[i] * rh
                if x1 - x0 >= 5:
                    fill = QColor('#c0392b'); fill.setAlpha(60)
                    p.setPen(QPen(QColor('#c0392b'), 1)); p.setBrush(fill)
                    p.drawRect(QRectF(x0, y + rh * 0.15, x1 - x0, rh * 0.7))
                else:
                    s = min(5.0, rh * 0.35)
                    poly = QPolygonF([QPointF(x0 - s, y + rh * 0.5 - s), QPointF(x0 + s, y + rh * 0.5 - s), QPointF(x0, y + rh * 0.5 + s)])
                    p.setPen(QPen(self.c_text(), 1)); p.setBrush(Qt.NoBrush)
                    p.drawPolygon(poly)
            # captures
            cp = sel[kk == KIND_CAPTURE]
            p.setPen(Qt.NoPen)
            for i in self._thin(cp, r, with_channel=True):
                x0, x1 = self._x(self.t0[i], r), self._x(self.t1[i], r)
                y = r.top() + self.row[i] * rh
                name = self.ch_names[self.ch[i]] if self.ch[i] >= 0 else None
                p.setBrush(self.ch_colors.get(name, QColor('#000000')))
                p.drawRect(QRectF(x0, y + rh * 0.12, max(2.2, x1 - x0), rh * 0.76))
            # hover outline
            if 0 <= self._hover < len(self.t0):
                i = self._hover
                x0, x1 = self._x(self.t0[i], r), self._x(self.t1[i], r)
                y = r.top() + self.row[i] * rh
                p.setPen(QPen(self.c_text(), 1.6)); p.setBrush(Qt.NoBrush)
                p.drawRect(QRectF(x0 - 1.5, y + 1, max(2.2, x1 - x0) + 3, rh - 2))
        p.setClipping(False)
        # axis labels (top)
        p.setFont(self.small_font(10))
        p.setPen(self.c_muted())
        t = first
        guard = 0
        last_x = -1e9
        while t <= self.v1 and guard < 400:
            x = self._x(t, r)
            lab = f'{t / unit:g}'
            w = QFontMetrics(p.font()).horizontalAdvance(lab)
            if x - w / 2 > last_x + 6:
                p.drawText(QPointF(x - w / 2, r.top() - 5), lab)
                last_x = x + w / 2
            guard += 1
            t += step
        p.drawText(QPointF(4, r.top() - 5), f'time ({ulabel})')
        # time point numbers (when there is room)
        if 0 < n_tp:
            label_every = max(1, int(math.ceil(34 / max(1e-9, px_per_tp)))) if px_per_tp < 1e8 else 1
            p.setFont(self.small_font(9))
            for tt, ix in zip(self.tp_t, self.tp_idx):
                if self.v0 <= tt <= self.v1 and ix % label_every == 0:
                    p.setPen(self.c_muted())
                    p.drawText(QPointF(self._x(tt, r) + 2, r.top() - 17), f'T{ix}')
        # legend
        self._paint_legend(p, r)
        p.setPen(QPen(self.c_grid(), 1)); p.setBrush(Qt.NoBrush)
        p.drawRect(r)

    def _thin(self, idx, r, with_channel):
        """never draw more than one mark per pixel column and row (zoomed-out views of long jobs)"""
        if len(idx) < 3000:
            return idx
        span = self.v1 - self.v0
        px = np.floor((self.t0[idx] - self.v0) / span * r.width()).astype(np.int64)
        key = (self.row[idx].astype(np.int64) * 100000 + px) * 64 + (self.ch[idx] + 1 if with_channel else 0)
        _, first = np.unique(key, return_index=True)
        return idx[np.sort(first)]

    def _paint_legend(self, p, r):
        p.setFont(self.small_font(10))
        fm = QFontMetrics(p.font())
        items = [(self.ch_colors.get(c, QColor('#000')), c, 'box') for c in self.used]
        if (self.k == KIND_AF).any():
            items.append((self.c_text(), 'autofocus', 'tri'))
        if len(self.tp_t):
            items.append((self.c_muted(), 'time point (red = late)', 'line'))
        x = self.LEFT
        y = 4
        for col, name, shape in items:
            w = fm.horizontalAdvance(name) + 24
            if x + w > self.width() - 6:
                break
            if shape == 'box':
                p.setPen(Qt.NoPen); p.setBrush(col); p.drawRect(QRectF(x, y + 3, 4, 10))
            elif shape == 'tri':
                p.setPen(QPen(col, 1)); p.setBrush(Qt.NoBrush)
                p.drawPolygon(QPolygonF([QPointF(x - 3, y + 4), QPointF(x + 5, y + 4), QPointF(x + 1, y + 11)]))
            else:
                p.setPen(QPen(col, 1, Qt.DashLine)); p.drawLine(QPointF(x + 1, y + 2), QPointF(x + 1, y + 13))
            p.setPen(self.c_text())
            p.drawText(QPointF(x + 10, y + 12), name)
            x += w

    # --- interaction
    def _hit(self, pos):
        """index into the arrays of the mark under the mouse (captures and autofocus first, then moves), -1 if none"""
        if not len(self.t0):
            return -1
        r = self._plot_rect()
        if not r.contains(pos):
            return -1
        rh = self._row_h()
        row = int((pos.y() - r.top()) / rh)
        tm = self._t(pos.x(), r)
        tol = 4.0 * (self.v1 - self.v0) / r.width()
        m = (self.row == row) & (self.t1 >= tm - tol) & (self.t0 <= tm + tol)
        best = -1
        for kinds in ((KIND_CAPTURE, KIND_AF), (KIND_MOVE,)):
            cand = np.where(m & np.isin(self.k, kinds))[0]
            if len(cand):
                d = np.abs((self.t0[cand] + self.t1[cand]) / 2 - tm)
                best = int(cand[np.argmin(d)])
                break
        return best

    def describe_mark(self, i):
        e = self.events[self.ev_index[i]]
        q = self.order[self.row[i]]
        what = {'capture': f'image {e.get("channel", "")}', 'autofocus': f'autofocus ({e.get("channel") or "AF channel"})', 'move': 'move'}[e['kind']]
        tp = f'time point {e["tp"]}' if e.get('tp') is not None else 'outside a time point loop'
        s = (f'<b>{what}</b><br>{tp}, position #{q.index}{" " + repr(q.id) if q.id else ""} (group {q.group})<br>'
             f't = {fmt_t(e["t0"])}  ({fmt_dur(e["t1"] - e["t0"])})')
        if e.get('line'):
            s += f'<br>line {e["line"]} in the job (click to go there)'
        return s

    def mouseMoveEvent(self, ev):
        pos = ev.pos()
        if self._press is not None and ev.buttons() & Qt.LeftButton:
            dx = pos.x() - self._press[0]
            if abs(dx) > 3 or self._moved:
                self._moved = True
                r = self._plot_rect()
                dt = -dx / r.width() * (self._press[2] - self._press[1])
                self.set_view(self._press[1] + dt, self._press[2] + dt)
                self.setCursor(Qt.ClosedHandCursor)
            return
        i = self._hit(QPointF(pos))
        if i != self._hover:
            self._hover = i
            self.update()
            self.pointHovered.emit(self.order[self.row[i]].index if i >= 0 else -1)
        if i >= 0:
            QToolTip.showText(ev.globalPos(), self.describe_mark(i), self)
            self.setCursor(Qt.PointingHandCursor if self.line[i] > 0 else Qt.ArrowCursor)
        else:
            tip = self._timepoint_tip(QPointF(pos))
            if tip:
                QToolTip.showText(ev.globalPos(), tip, self)
            else:
                QToolTip.hideText()
            self.setCursor(Qt.ArrowCursor)

    def _timepoint_tip(self, pos):
        r = self._plot_rect()
        if not len(self.tp_t) or not r.contains(pos):
            return None
        tm = self._t(pos.x(), r)
        tol = 4.0 * (self.v1 - self.v0) / r.width()
        j = int(np.argmin(np.abs(self.tp_t - tm)))
        if abs(self.tp_t[j] - tm) <= tol:
            late = self.tp_late[j]
            return f'<b>time point {self.tp_idx[j]}</b> starts at {fmt_t(self.tp_t[j])}' + (f'<br><span style="color:#d62728">{late:.0f} s later than planned</span>' if late > 0.5 else '')
        return None

    def leaveEvent(self, ev):
        if self._hover != -1:
            self._hover = -1
            self.pointHovered.emit(-1)
            self.update()

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._press = (ev.pos().x(), self.v0, self.v1)
            self._moved = False

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.LeftButton and self._press is not None:
            if not self._moved:
                i = self._hit(QPointF(ev.pos()))
                if i >= 0 and self.line[i] > 0:
                    self.eventClicked.emit(int(self.line[i]))
            self._press = None
            self.setCursor(Qt.ArrowCursor)

    def mouseDoubleClickEvent(self, ev):
        self.fit()

    def wheelEvent(self, ev):
        if not self.events:
            return
        r = self._plot_rect()
        factor = 0.8 ** (ev.angleDelta().y() / 120.0)
        tm = self._t(ev.pos().x(), r)
        span = (self.v1 - self.v0) * factor
        span = min(max(span, 0.5), self.t_end * 1.5 + 1)
        frac = (tm - self.v0) / (self.v1 - self.v0)
        self.set_view(tm - frac * span, tm - frac * span + span)
        ev.accept()


# ------------------------------------------------------------------------------------------------
class StageMapView(_View):
    pointHovered = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(190, 150)
        self._hover = -1
        self._ext_highlight = -1
        self.grp_colors = {}
        self._screen = {}

    def _data_changed(self):
        self.ch_colors, self.used, self.grp_colors = color_maps(self.points, self.events, getattr(self, 'channel_order', None))
        self._hover = -1

    def highlight_point(self, index):
        if index != self._ext_highlight:
            self._ext_highlight = index
            self.update()

    def _bounds(self):
        xs = [q.x for q in self.points]; ys = [q.y for q in self.points]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        if x1 - x0 < 1e-6:
            x0, x1 = x0 - 0.5, x1 + 0.5
        if y1 - y0 < 1e-6:
            y0, y1 = y0 - 0.5, y1 + 0.5
        return x0, x1, y0, y1

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), self.c_bg())
        self._title(p, 'Stage map (mm)')
        if not self.points:
            self._paint_message(p)
            return
        left, top, right, bottom = 38, 24, 10, 22
        area = QRectF(left, top, max(20, self.width() - left - right), max(20, self.height() - top - bottom))
        x0, x1, y0, y1 = self._bounds()
        s = min(area.width() / (x1 - x0), area.height() / (y1 - y0)) * 0.92
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        ox, oy = area.center().x(), area.center().y()
        sx = lambda x: ox + (x - cx) * s
        sy = lambda y: oy - (y - cy) * s
        self._screen = {q.index: QPointF(sx(q.x), sy(q.y)) for q in self.points}
        p.setPen(QPen(self.c_grid(), 1)); p.setBrush(Qt.NoBrush)
        p.drawRect(area)
        p.setFont(self.small_font(9)); p.setPen(self.c_muted())
        p.drawText(QPointF(area.left(), area.bottom() + 12), f'x {x0:.2f}')
        p.drawText(QPointF(area.right() - 60, area.bottom() + 12), f'{x1:.2f}')
        p.drawText(QPointF(2, area.bottom()), f'{y0:.2f}')
        p.drawText(QPointF(2, area.top() + 9), f'{y1:.2f}')
        # visiting order in the first time point that images something
        first_tp = next((e['tp'] for e in self.events if e['kind'] == 'capture'), None)
        seen, order = set(), []
        for e in self.events:
            if e['kind'] in ('move', 'capture') and e['tp'] == first_tp and e.get('point') is not None and e['point'] not in seen and e['point'] in self._screen:
                seen.add(e['point']); order.append(e['point'])
        if len(order) > 1:
            p.setPen(QPen(self.c_grid(), 1.2))
            for a, b in zip(order, order[1:]):
                p.drawLine(self._screen[a], self._screen[b])
        imaged = {e['point'] for e in self.events if e['kind'] == 'capture'}
        for q in self.points:
            pt = self._screen[q.index]
            col = self.grp_colors.get(q.group, QColor('#888'))
            rad = 4.5 if q.index != self._ext_highlight else 7
            if self.events and q.index not in imaged:
                p.setPen(QPen(QColor('#d62728'), 1.5)); p.setBrush(Qt.NoBrush)
                p.drawEllipse(pt, rad + 3, rad + 3)
            p.setPen(QPen(self.c_text(), 1.5 if q.index == self._ext_highlight else 0.5))
            p.setBrush(col)
            p.drawEllipse(pt, rad, rad)
        if len(self.points) <= 60:
            p.setFont(self.small_font(9)); p.setPen(self.c_text())
            for q in self.points:
                p.drawText(self._screen[q.index] + QPointF(5, -5), str(q.index))
        # legend: groups
        y = top + 12
        p.setFont(self.small_font(10))
        for g, col in self.grp_colors.items():
            n = sum(1 for q in self.points if q.group == g)
            p.setPen(Qt.NoPen); p.setBrush(col)
            p.drawEllipse(QPointF(area.left() + 10, y - 4), 4, 4)
            p.setPen(self.c_text())
            p.drawText(QPointF(area.left() + 18, y), f'{g} ({n})')
            y += 13
        if self.events and any(q.index not in imaged for q in self.points):
            p.setPen(QColor('#d62728'))
            p.drawText(QPointF(area.left() + 6, area.bottom() - 6), 'red ring: never imaged')

    def _nearest(self, pos):
        best, bd = -1, 10.0
        for i, pt in self._screen.items():
            d = math.hypot(pt.x() - pos.x(), pt.y() - pos.y())
            if d < bd:
                best, bd = i, d
        return best

    def mouseMoveEvent(self, ev):
        i = self._nearest(ev.pos())
        if i != self._hover:
            self._hover = i
            self.pointHovered.emit(i)
        if i >= 0:
            q = next(q for q in self.points if q.index == i)
            QToolTip.showText(ev.globalPos(), f'<b>#{q.index} {q.id or "(no ID)"}</b><br>group {q.group}<br>x {q.x:.3f}, y {q.y:.3f} mm' + (f', z {q.z:.4f}' if q.z is not None else ''), self)
        else:
            QToolTip.hideText()

    def leaveEvent(self, ev):
        if self._hover != -1:
            self._hover = -1
            self.pointHovered.emit(-1)


# ------------------------------------------------------------------------------------------------
class BudgetView(_View):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(190, 150)
        self.tps, self.acc, self.interval, self.work = [], {}, None, np.zeros(0)
        self._bars = []

    def _data_changed(self):
        self.tps = sorted({e['tp'] for e in self.events if e.get('tp') is not None})
        pos = {t: i for i, t in enumerate(self.tps)}
        self.acc = {k: np.zeros(len(self.tps)) for k in ('move', 'capture', 'autofocus')}
        for e in self.events:
            if e.get('tp') in pos and e['kind'] in self.acc:
                self.acc[e['kind']][pos[e['tp']]] += e['t1'] - e['t0']
        self.work = sum(self.acc.values()) if self.tps else np.zeros(0)
        starts = [e['t0'] for e in self.events if e['kind'] == 'timepoint']
        self.interval = None
        if len(starts) > 1:
            diffs = np.diff(starts)
            self.interval = float(np.median(diffs))

    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), self.c_bg())
        self._title(p, 'Working time per time point')
        if not self.events or not self.tps:
            if self.message:
                self._paint_message(p)
            return
        left, top, right, bottom = 44, 24, 8, 20
        area = QRectF(left, top, max(20, self.width() - left - right), max(20, self.height() - top - bottom))
        ymax = max(float(self.work.max()), self.interval or 0) * 1.12 or 1.0
        unit, ulabel = axis_unit(ymax)
        n = len(self.tps)
        bw = area.width() / n
        self._bars = []
        p.setFont(self.small_font(9))
        step = nice_step(ymax, 4)
        t = 0.0
        while t <= ymax:
            y = area.bottom() - t / ymax * area.height()
            p.setPen(QPen(self.c_grid(), 1)); p.drawLine(QPointF(area.left(), y), QPointF(area.right(), y))
            p.setPen(self.c_muted()); p.drawText(QRectF(0, y - 7, left - 4, 14), Qt.AlignRight | Qt.AlignVCenter, f'{t / unit:g}')
            t += step
        late = 0
        for i in range(n):
            x = area.left() + i * bw
            bottom_y = area.bottom()
            for k in ('move', 'capture', 'autofocus'):
                h = self.acc[k][i] / ymax * area.height()
                p.fillRect(QRectF(x, bottom_y - h, max(1.0, bw - (1 if bw > 4 else 0)), h), QColor(BUDGET_COLORS[k]))
                bottom_y -= h
            self._bars.append((x, bw))
            if self.interval and self.work[i] > self.interval * 1.001:
                late += 1
                p.fillRect(QRectF(x, area.top() - 0, max(1.0, bw - 1), 3), QColor('#d62728'))
        if self.interval:
            y = area.bottom() - self.interval / ymax * area.height()
            p.setPen(QPen(self.c_text(), 1, Qt.DashLine)); p.drawLine(QPointF(area.left(), y), QPointF(area.right(), y))
            p.setFont(self.small_font(9)); p.setPen(self.c_text())
            p.drawText(QPointF(area.left() + 4, y - 3), f'interval {fmt_dur(self.interval)}')
        p.setPen(self.c_muted()); p.setFont(self.small_font(9))
        p.drawText(QPointF(area.left(), self.height() - 5), f'time point 0 ... {self.tps[-1]}   (axis: {ulabel})')
        if late:
            p.setPen(QColor('#d62728'))
            p.drawText(QRectF(area.left(), area.top() + 4, area.width() - 4, 14), Qt.AlignRight, f'{late} time point(s) take longer than the interval')

    def mouseMoveEvent(self, ev):
        if not self._bars:
            return
        x = ev.pos().x()
        i = next((j for j, (bx, bw) in enumerate(self._bars) if bx <= x < bx + max(bw, 1)), -1)
        if i < 0:
            QToolTip.hideText()
            return
        txt = (f'<b>time point {self.tps[i]}</b><br>working: {fmt_dur(self.work[i])}<br>'
               f'moving {fmt_dur(self.acc["move"][i])}, imaging {fmt_dur(self.acc["capture"][i])}, autofocus {fmt_dur(self.acc["autofocus"][i])}')
        if self.interval:
            txt += f'<br>interval {fmt_dur(self.interval)}' + (' <span style="color:#d62728">(too long!)</span>' if self.work[i] > self.interval * 1.001 else '')
        QToolTip.showText(ev.globalPos(), txt, self)


# ------------------------------------------------------------------------------------------------
class MatrixView(_View):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(190, 110)
        self.rows, self.tps, self.m = [], [], np.zeros((0, 0))
        self._geom = None

    def _data_changed(self):
        self.tps = sorted({e['tp'] for e in self.events if e.get('tp') is not None})
        used = []
        for e in self.events:
            if e['kind'] == 'capture' and e['channel'] not in used:
                used.append(e['channel'])
        self.rows = used + ['autofocus']
        pos = {t: i for i, t in enumerate(self.tps)}
        self.m = np.zeros((len(self.rows), len(self.tps)))
        for e in self.events:
            if e.get('tp') not in pos:
                continue
            if e['kind'] == 'capture':
                self.m[self.rows.index(e['channel']), pos[e['tp']]] += 1
            elif e['kind'] == 'autofocus':
                self.m[-1, pos[e['tp']]] += 1

    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), self.c_bg())
        self._title(p, 'Images / autofocus per time point')
        if not self.events or not self.tps:
            if self.message:
                self._paint_message(p)
            return
        fm = QFontMetrics(self.small_font(10))
        left = min(120, max(fm.horizontalAdvance(r) for r in self.rows) + 10)
        top, right, bottom = 24, 8, 16
        area = QRectF(left, top, max(20, self.width() - left - right), max(20, self.height() - top - bottom))
        nr, nt = self.m.shape
        cw, rh = area.width() / nt, area.height() / nr
        self._geom = (area, cw, rh)
        vmax = max(1.0, self.m.max())
        p.setFont(self.small_font(10))
        base = QColor('#4c72b0')
        for i in range(nr):
            p.setPen(self.c_text())
            p.drawText(QRectF(2, area.top() + i * rh, left - 6, rh), Qt.AlignVCenter | Qt.AlignRight, fm.elidedText(self.rows[i], Qt.ElideLeft, left - 8))
            if cw < 1.0:                     # more time points than pixels: one column per pixel, the maximum of its time points
                ncol = int(area.width())
                for c in range(ncol):
                    a, b = int(c * nt / ncol), max(int(c * nt / ncol) + 1, int((c + 1) * nt / ncol))
                    v = self.m[i, a:b].max()
                    if v:
                        col = QColor(base); col.setAlphaF(0.15 + 0.85 * v / vmax)
                        p.fillRect(QRectF(area.left() + c, area.top() + i * rh + 1, 1.0, rh - 2), col)
            else:
                for j in range(nt):
                    v = self.m[i, j]
                    rect = QRectF(area.left() + j * cw, area.top() + i * rh + 1, max(1.0, cw - (1 if cw > 4 else 0)), rh - 2)
                    if v:
                        col = QColor(base); col.setAlphaF(0.15 + 0.85 * v / vmax)
                        p.fillRect(rect, col)
                        if cw >= 18 and rh >= 13:
                            p.setPen(QColor('white') if v > 0.6 * vmax else self.c_text())
                            p.drawText(rect, Qt.AlignCenter, f'{int(v)}')
                    else:
                        p.fillRect(rect, QColor(128, 128, 128, 18))
        p.setFont(self.small_font(9)); p.setPen(self.c_muted())
        p.drawText(QPointF(area.left(), self.height() - 3), f'time point 0 ... {self.tps[-1]}')

    def mouseMoveEvent(self, ev):
        if not self._geom or not self.tps:
            return
        area, cw, rh = self._geom
        pos = ev.pos()
        if not area.contains(QPointF(pos)):
            QToolTip.hideText()
            return
        i = int((pos.y() - area.top()) / rh)
        j = int((pos.x() - area.left()) / cw) if cw >= 1 else int((pos.x() - area.left()) * len(self.tps) / area.width())
        if 0 <= i < len(self.rows) and 0 <= j < len(self.tps):
            what = 'autofocus runs' if self.rows[i] == 'autofocus' else f'images in {self.rows[i]}'
            QToolTip.showText(ev.globalPos(), f'time point {self.tps[j]}: {int(self.m[i, j])} {what}', self)
