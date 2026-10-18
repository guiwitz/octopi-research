"""Graphical preview of a job: shows WHAT the job would do and WHEN, so it can be checked before it is started.

The job is run in a dry run (simulated time, no hardware) with recording switched on; the recorded operations are drawn:

  1. Timeline per position: one row per position (grouped), one coloured tick per image (colour = channel),
     triangles = autofocus, dashed lines = start of each time point (red = started late).
  2. Detail of the first time point (or any time range you choose) to see the order of channels, autofocus and moves.
  3. Stage map: where the positions are, coloured by group, with the order in which they are visited.
  4. Time budget per time point: moving / imaging / autofocus / waiting, against the planned interval.
  5. Overview matrix: images per channel and autofocus runs for each time point (e.g. "BF only every 4th time point").

The times come from the dry run (estimated durations of moves, exposures and autofocus), not from measurements; they are
good enough to see order and structure, and a rough check of the interval. Use plot_job(...) from Python or
`python -m control.jobs.plot job.py positions.csv` from the software folder.
"""
import os
import sys

import numpy as np

from control.jobs.runner import dry_run, describe, JobResult

CHANNEL_COLORS = ['#1f77b4', '#2ca02c', '#d62728', '#9467bd', '#ff7f0e', '#17becf', '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22']
GROUP_COLORS = ['#4c72b0', '#dd8452', '#55a868', '#c44e52', '#8172b3', '#937860', '#da8bc3', '#8c8c8c']
BUDGET_COLORS = dict(move='#bbbbbb', capture='#4c72b0', autofocus='#c44e52', wait='#eeeeee')


def _unit(t_max):
    if t_max >= 2 * 3600:
        return 3600.0, 'h'
    if t_max >= 120:
        return 60.0, 'min'
    return 1.0, 's'


def _channel_color_map(events, channel_order=None):
    names = list(channel_order or [])
    for e in events:
        if e.get('channel') and e['channel'] not in names:
            names.append(e['channel'])
    return {c: CHANNEL_COLORS[i % len(CHANNEL_COLORS)] for i, c in enumerate(names)}


def _group_color_map(points):
    groups = []
    for p in points:
        if p.group not in groups:
            groups.append(p.group)
    return {g: GROUP_COLORS[i % len(GROUP_COLORS)] for i, g in enumerate(groups)}


def _timeline(ax, events, points, ch_color, grp_color, t_range, unit, ulabel, title, max_labels=30):
    """one row per point; rows are ordered by group, then table order"""
    order = sorted(points, key=lambda p: (list(grp_color).index(p.group), p.index))
    row = {p.index: i for i, p in enumerate(order)}
    t0, t1 = t_range
    n_tp = sum(1 for e in events if e['kind'] == 'timepoint')
    for e in events:
        if e['kind'] == 'timepoint' and t0 <= e['t0'] <= t1:
            late = e.get('late', 0) > 0.5
            ax.axvline(e['t0'] / unit, color='#d62728' if late else '#bbbbbb', lw=1.0 if late else 0.5,
                       ls='-' if n_tp > 40 else '--', alpha=0.9 if late else 0.7, zorder=0)
    xs, ys, cs = [], [], []
    ax_x, ax_y = [], []
    for e in events:
        if e['point'] is None or e['t1'] < t0 or e['t0'] > t1:
            continue
        r = row.get(e['point'])
        if r is None:
            continue
        if e['kind'] == 'capture':
            xs.append(e['t0'] / unit); ys.append(r); cs.append(ch_color.get(e['channel'], 'k'))
        elif e['kind'] == 'autofocus':
            ax_x.append(e['t0'] / unit); ax_y.append(r)
    detail = (t1 - t0) < 600
    ax.scatter(xs, ys, c=cs, marker='|', s=90 if detail else 60, linewidths=1.6 if detail else 1.1, zorder=3)
    ax.scatter(ax_x, ax_y, marker='v', s=26 if detail else 14, facecolors='none', edgecolors='k', linewidths=0.8, zorder=4)
    ax.set_ylim(len(order) - 0.5, -0.5)
    ax.set_xlim(t0 / unit, t1 / unit)
    step = max(1, int(np.ceil(len(order) / max_labels)))
    ax.set_yticks(range(0, len(order), step))
    ax.set_yticklabels([(order[i].id or f'#{order[i].index}') for i in range(0, len(order), step)], fontsize=7)
    for lab, i in zip(ax.get_yticklabels(), range(0, len(order), step)):
        lab.set_color(grp_color[order[i].group])
    # separators between groups
    for i in range(1, len(order)):
        if order[i].group != order[i - 1].group:
            ax.axhline(i - 0.5, color='#cccccc', lw=0.8, zorder=0)
    ax.set_xlabel(f'time ({ulabel})')
    ax.set_title(title, fontsize=9, loc='left')
    ax.grid(axis='x', color='#eeeeee', lw=0.6)


def _stage_map(ax, points, grp_color, events):
    if not points:
        return
    # visiting order of the first time point that images something
    first_tp = next((e['tp'] for e in events if e['kind'] == 'capture'), None)
    seen, order = set(), []
    for e in events:
        if e['kind'] in ('move', 'capture') and e['tp'] == first_tp and e['point'] is not None and e['point'] not in seen:
            seen.add(e['point']); order.append(e['point'])
    by_idx = {p.index: p for p in points}
    if len(order) > 1:
        ax.plot([by_idx[i].x for i in order], [by_idx[i].y for i in order], '-', color='#bbbbbb', lw=0.8, zorder=1)
    for g, c in grp_color.items():
        gp = [p for p in points if p.group == g]
        ax.scatter([p.x for p in gp], [p.y for p in gp], s=34, color=c, label=f'{g} ({len(gp)})', zorder=2)
    if len(points) <= 60:
        for p in points:
            ax.annotate(str(p.index), (p.x, p.y), xytext=(3, 3), textcoords='offset points', fontsize=6)
    imaged = {e['point'] for e in events if e['kind'] == 'capture'}
    unvisited = [p for p in points if p.index not in imaged]
    if unvisited:
        ax.scatter([p.x for p in unvisited], [p.y for p in unvisited], s=70, facecolors='none', edgecolors='#d62728', lw=1.0, zorder=3,
                   label='never imaged by this job')
    ax.set_aspect('equal', adjustable='datalim')
    ax.set_xlabel('x (mm)'); ax.set_ylabel('y (mm)')
    ax.set_title('Stage map (line: order in the first time point)', fontsize=9, loc='left')
    ax.legend(fontsize=7, loc='best', frameon=False)
    ax.grid(color='#eeeeee', lw=0.6)


def _budget(ax, events, interval):
    tps = sorted({e['tp'] for e in events if e['tp'] is not None})
    if not tps:
        ax.text(0.5, 0.5, 'no time points', ha='center', va='center', transform=ax.transAxes)
        ax.set_axis_off()
        return
    acc = {k: np.zeros(len(tps)) for k in ('move', 'capture', 'autofocus', 'wait')}
    pos = {t: i for i, t in enumerate(tps)}
    # time points start at 'timepoint' events; waits before the next start belong to the previous time point's slack
    for e in events:
        if e['tp'] is None or e['kind'] not in acc:
            continue
        acc[e['kind']][pos[e['tp']]] += e['t1'] - e['t0']
    bottom = np.zeros(len(tps))
    x = np.arange(len(tps))
    for k in ('move', 'capture', 'autofocus'):
        ax.bar(x, acc[k] / 60.0, bottom=bottom, color=BUDGET_COLORS[k], label=k, width=0.85, edgecolor='none')
        bottom += acc[k] / 60.0
    starts = [e['t0'] for e in events if e['kind'] == 'timepoint']
    work = acc['move'] + acc['capture'] + acc['autofocus']
    if interval is None and len(starts) > 1:
        interval = float(np.median(np.diff(starts)))
    if interval:
        ax.axhline(interval / 60.0, color='k', ls='--', lw=0.9, label=f'interval {interval / 60:.1f} min')
        late = int((work > interval).sum())
        if late:
            ax.text(0.01, 0.80, f'{late} time point(s) take longer than the interval', color='#d62728', fontsize=7, transform=ax.transAxes, va='top')
    ax.set_xlabel('time point'); ax.set_ylabel('minutes')
    ax.set_title('Working time per time point (waiting not shown)', fontsize=9, loc='left')
    ax.legend(fontsize=7, frameon=False, ncol=2, loc='center right')
    if len(tps) <= 30:
        ax.set_xticks(x); ax.set_xticklabels([str(t) for t in tps], fontsize=7)


def _matrix(ax, events, channels):
    tps = sorted({e['tp'] for e in events if e['tp'] is not None})
    rows = list(channels) + ['autofocus']
    if not tps:
        ax.set_axis_off()
        return
    m = np.zeros((len(rows), len(tps)))
    pos = {t: i for i, t in enumerate(tps)}
    for e in events:
        if e['tp'] is None:
            continue
        if e['kind'] == 'capture':
            m[rows.index(e['channel']), pos[e['tp']]] += 1
        elif e['kind'] == 'autofocus':
            m[-1, pos[e['tp']]] += 1
    im = ax.imshow(m, aspect='auto', cmap='Blues', vmin=0, vmax=max(1, m.max()), interpolation='nearest',
                   extent=(tps[0] - 0.5, tps[-1] + 0.5, len(rows) - 0.5, -0.5))
    ax.set_yticks(range(len(rows))); ax.set_yticklabels(rows, fontsize=7)
    ax.set_xlabel('time point')
    ax.set_title('Images per channel / autofocus runs per time point', fontsize=9, loc='left')
    if len(tps) * len(rows) <= 150:
        for i in range(len(rows)):
            for j in range(len(tps)):
                if m[i, j]:
                    ax.text(tps[j], i, f'{int(m[i, j])}', ha='center', va='center', fontsize=6, color='white' if m[i, j] > 0.6 * m.max() else 'k')
    return im


def plot_job(result, points, channels=None, detail_range=None, interval=None, title=None):
    """Draw the figure for a recorded dry-run `result` (see preview_job). Returns a matplotlib Figure
    (not shown, not saved). detail_range=(t_start_s, t_end_s) selects the time range of the detail panel
    (default: the first time point)."""
    from matplotlib.figure import Figure
    from matplotlib.lines import Line2D
    events = result.events or []
    fig = Figure(figsize=(14, 12), dpi=100)
    gs = fig.add_gridspec(3, 3, height_ratios=[1.6, 1.3, 1.15], hspace=0.55, wspace=0.28)
    ch_color = _channel_color_map(events, channels)
    used = {e['channel'] for e in events if e['kind'] == 'capture'}
    ch_used = {c: col for c, col in ch_color.items() if c in used}
    grp_color = _group_color_map(points)
    status = result.status if result.status != 'completed' else 'dry run OK'
    head = title or f'{status}: {result.stats.get("images", 0)} images, {result.stats.get("autofocus", 0)} autofocus runs, ' \
                    f'{result.stats.get("timepoints", 0)} time points, estimated duration {_fmt_dur(result.duration_s)}'
    fig.suptitle(head, fontsize=11, x=0.01, ha='left', color='#d62728' if result.status not in ('completed',) else 'k')
    if not events:
        ax = fig.add_subplot(gs[:, :])
        ax.set_axis_off()
        ax.text(0.5, 0.5, 'The job did nothing (no images, no moves).' + (f'\n\n{result.error}' if result.error else ''),
                ha='center', va='center', fontsize=11, transform=ax.transAxes)
        return fig
    t_end = max(e['t1'] for e in events)
    unit, ulabel = _unit(t_end)
    ax1 = fig.add_subplot(gs[0, :])
    _timeline(ax1, events, points, ch_color, grp_color, (0.0, t_end * 1.001 + 1e-9), unit, ulabel,
              'Timeline per position  (ticks: images, colour = channel;  triangles: autofocus;  dashed: time points, red = late)')
    if detail_range is None:
        starts = [e['t0'] for e in events if e['kind'] == 'timepoint']
        if len(starts) > 1:
            first_end = max([e['t1'] for e in events if e['tp'] == 0 and e['kind'] != 'wait'] or [starts[1]])
            detail_range = (starts[0], first_end + 0.02 * (first_end - starts[0] + 1))
        else:
            detail_range = (0.0, t_end * 1.001 + 1e-9)
    du, dl = _unit(detail_range[1] - detail_range[0])
    ax2 = fig.add_subplot(gs[1, :])
    _timeline(ax2, events, points, ch_color, grp_color, detail_range, du, dl,
              f'Detail: {detail_range[0] / du:.1f} - {detail_range[1] / du:.1f} {dl}  (first time point)' if detail_range is not None else 'Detail')
    handles = [Line2D([0], [0], marker='|', color=c, lw=0, markersize=9, label=n) for n, c in ch_used.items()]
    handles.append(Line2D([0], [0], marker='v', mfc='none', mec='k', lw=0, markersize=6, label='autofocus'))
    ax1.legend(handles=handles, fontsize=7, ncol=min(8, len(handles)), loc='lower right', frameon=True, framealpha=0.9)
    _stage_map(fig.add_subplot(gs[2, 0]), points, grp_color, events)
    _budget(fig.add_subplot(gs[2, 1]), events, interval)
    _matrix(fig.add_subplot(gs[2, 2]), events, list(ch_used))
    foot = ('Times are ESTIMATES from a dry run (assumed durations for moves/autofocus, exposure times from the channel settings). '
            + ('; '.join([w for w in result.warnings if 'empty image' not in w][:2])))
    fig.text(0.01, 0.005, foot[:260], fontsize=6.5, color='#666666', va='bottom')
    return fig


def _fmt_dur(s):
    return f'{s / 3600:.1f} h' if s >= 7200 else f'{s / 60:.1f} min'


def preview_job(job_path, points, channel_infos, out_path=None, **kw):
    """Dry-run the job (recording), draw it, optionally save it (png/pdf/svg). Returns (figure, JobResult)."""
    res = dry_run(job_path, points, channel_infos, record=True)
    fig = plot_job(res, points, channels=list(channel_infos), **kw)
    if out_path:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        FigureCanvasAgg(fig)
        fig.savefig(out_path, bbox_inches='tight')
    return fig, res


# ------------------------------------------------------------------------------------------------
def _read_positions(path):
    """positions.csv with columns id,x_mm,y_mm[,z_mm]  (also reads the positions_initial.csv written by a job)"""
    import csv
    ids, coords = [], []
    with open(path, newline='', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            ids.append(r.get('id', ''))
            coords.append((float(r['x_mm']), float(r['y_mm']), float(r['z_mm']) if r.get('z_mm') not in (None, '', 'None') else None))
    return ids, [tuple(c) if c[2] is not None else c[:2] for c in coords]


def main(argv=None):
    import argparse
    from control.jobs.api import make_points
    ap = argparse.ArgumentParser(description='Draw what a job would do (dry run, no hardware).')
    ap.add_argument('job')
    ap.add_argument('positions', help='csv with columns id,x_mm,y_mm[,z_mm] (e.g. positions_initial.csv of an earlier job)')
    ap.add_argument('--channels', default='BF,488,561', help='comma separated channel names available to the job')
    ap.add_argument('--exposure-ms', type=float, default=50.0)
    ap.add_argument('--out', default='job_plan.png')
    a = ap.parse_args(argv)
    ids, coords = _read_positions(a.positions)
    infos = {c: dict(exposure_ms=a.exposure_ms, gain=0, intensity=50) for c in a.channels.split(',')}
    pts = make_points(ids, coords)
    fig, res = preview_job(a.job, pts, infos, out_path=a.out)
    print(describe(res))
    print('figure written to', os.path.abspath(a.out))
    return 0 if res.status == 'completed' else 1


if __name__ == '__main__':
    sys.exit(main())
