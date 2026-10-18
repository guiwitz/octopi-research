"""Confirmation window shown before a job or a multipoint acquisition starts: lists the illumination / camera settings
that will be used (per channel: exposure time, analog gain, illumination intensity and source, plus the trigger mode),
so a wrong setting is noticed before hours of imaging. OK starts, Cancel does not.

Set the environment variable SQUID_SKIP_CONFIRM=1 (or acq_summary.ENABLED = False) to skip the window (automated tests).
"""
import os
import traceback

ENABLED = True


def _fmt(v, nd=2):
    if v is None:
        return '-'
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return ('%.*f' % (nd, f)).rstrip('0').rstrip('.') if nd else str(int(round(f)))


def trigger_description(mode):
    m = str(mode)
    if 'Hardware' in m:
        return 'Hardware trigger  (exposure and illumination timed by the microcontroller)'
    if 'Software' in m:
        return 'Software trigger  (illumination on, then the camera is triggered by software)'
    return m or 'unknown'


def summary_rows(configurations, names):
    """[(channel, exposure ms, analog gain, illumination intensity, illumination source)] for the given channel names,
    in the order of `names`. Pure logic, no Qt."""
    by_name = {c.name: c for c in configurations}
    rows = []
    for n in names:
        c = by_name.get(n)
        if c is None:
            rows.append((n, '?', '?', '?', '?'))
        else:
            rows.append((n, _fmt(c.exposure_time), _fmt(c.analog_gain), _fmt(c.illumination_intensity), _fmt(getattr(c, 'illumination_source', None), 0)))
    return rows


def summary_text(configurations, names, trigger_mode):
    lines = ['Trigger mode: ' + trigger_description(trigger_mode), '']
    lines.append('%-32s %12s %10s %14s %8s' % ('channel', 'exposure ms', 'gain', 'intensity', 'source'))
    for r in summary_rows(configurations, names):
        lines.append('%-32s %12s %10s %14s %8s' % r)
    return '\n'.join(lines)


def confirm_acquisition(parent, what, configurations, names, trigger_mode, extra=None):
    """Show the window; True = OK, False = Cancel. `what` e.g. 'job'. `extra`: optional additional line of text."""
    if not ENABLED or os.environ.get('SQUID_SKIP_CONFIRM'):
        return True
    from qtpy.QtWidgets import (QDialog, QVBoxLayout, QLabel, QTableWidget, QTableWidgetItem, QDialogButtonBox,
                                QHeaderView, QAbstractItemView)
    from qtpy.QtCore import Qt
    dlg = QDialog(parent)
    dlg.setWindowTitle('Start ' + what + '? - check the imaging conditions')
    lay = QVBoxLayout(dlg)
    head = QLabel('These settings will be used for the ' + what + ':')
    head.setWordWrap(True)
    lay.addWidget(head)
    trig = QLabel('<b>Trigger mode:</b> ' + trigger_description(trigger_mode))
    trig.setWordWrap(True)
    lay.addWidget(trig)
    rows = summary_rows(configurations, names)
    table = QTableWidget(len(rows), 5)
    table.setHorizontalHeaderLabels(['Channel', 'Exposure (ms)', 'Analog gain', 'Illumination intensity', 'Source'])
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setSelectionMode(QAbstractItemView.NoSelection)
    table.verticalHeader().setVisible(False)
    for i, r in enumerate(rows):
        for j, v in enumerate(r):
            it = QTableWidgetItem(str(v))
            if j:
                it.setTextAlignment(Qt.AlignCenter)
            table.setItem(i, j, it)
    table.resizeColumnsToContents()
    table.horizontalHeader().setStretchLastSection(True)
    table.setMinimumHeight(min(400, 40 + 26 * len(rows)))
    table.setMinimumWidth(min(900, sum(table.columnWidth(c) for c in range(5)) + 30))
    lay.addWidget(table)
    if extra:
        e = QLabel(extra)
        e.setWordWrap(True)
        lay.addWidget(e)
    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.button(QDialogButtonBox.Ok).setText('OK, start')
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    lay.addWidget(buttons)
    return dlg.exec_() == QDialog.Accepted


def confirm_start(parent, what, controller, names, extra=None):
    """Convenience for widgets: takes the settings from a MultiPointController. A failure in this window never blocks an
    acquisition (it is printed and the run goes on)."""
    try:
        return confirm_acquisition(parent, what, controller.configurationManager.configurations, list(names),
                                   controller.liveController.trigger_mode, extra)
    except Exception:
        traceback.print_exc()
        return True
