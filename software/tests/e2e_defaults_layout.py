"""Startup defaults and control-panel layout (simulation, offscreen).

  SQUID_E2E_OFFSCREEN=1 python tests/e2e_defaults_layout.py
Checks: objective 10x, 12-well format, all channels 10 ms, AF 10 planes / 2.5 um; JOBs / Single measurement text fields
wide enough; the control panel can be switched to two columns and back (widgets keep their parent, width changes).
"""
import os, sys, time, tempfile
SOFTWARE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(SOFTWARE_DIR); sys.path.insert(0, SOFTWARE_DIR)
os.environ['QT_API'] = 'pyqt5'
if os.environ.get('SQUID_E2E_OFFSCREEN'):
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    from unittest.mock import MagicMock
    for m in ['napari', 'napari.utils', 'napari.utils.colormaps']:
        sys.modules[m] = MagicMock()
    import control._def as d
    d.USE_NAPARI_FOR_LIVE_VIEW = d.USE_NAPARI_FOR_MULTIPOINT = d.USE_NAPARI_FOR_TILED_DISPLAY = False
from qtpy.QtWidgets import QApplication
import control.gui_hcs as gui

results = []
def check(name, ok, detail=''):
    results.append(bool(ok)); print(('  PASS  ' if ok else '  FAIL  ') + name + (f'   [{detail}]' if detail and not ok else ''), flush=True)

# do not depend on / change the user's saved layout preference
pref = gui.OctopiGUI.LAYOUT_PREFERENCE_FILE
backup = open(pref).read() if os.path.exists(pref) else None
if os.path.exists(pref): os.remove(pref)
try:
    app = QApplication([]); app.setStyle('Fusion')
    win = gui.OctopiGUI(is_simulation=True)
    win.resize(1900, 1000); win.show(); app.processEvents()

    print('defaults')
    check('objective 10x', win.objectiveStore.current_objective == '10x' and win.objectivesWidget.dropdown.currentText() == '10x', win.objectiveStore.current_objective)
    check('well plate format 12', win.wellplateFormatWidget.comboBox.currentData() == 12 and gui.WELLPLATE_FORMAT == 12, win.wellplateFormatWidget.comboBox.currentData())
    exps = {c.name: c.exposure_time for c in win.configurationManager.configurations}
    check('all channels 10 ms (objects)', all(v == 10 for v in exps.values()), exps)
    check('all channels 10 ms (xml tree)', all(float(m.get('ExposureTime')) == 10 for m in win.configurationManager.config_xml_tree_root.iter('mode')))
    afc = win.autofocusController
    check('AF 10 planes', afc.N == 10 and win.autofocusWidget.entry_N.value() == 10, afc.N)
    check('AF 2.5 um', abs(afc.deltaZ * 1000 - 2.5) < 0.05 and abs(win.autofocusWidget.entry_delta.value() - 2.5) < 0.05, afc.deltaZ * 1000)
    live = win.liveControlWidget
    check('live control shows 10 ms', abs(live.entry_exposureTime.value() - 10) < 1e-6 if hasattr(live, 'entry_exposureTime') else True)

    print('text fields')
    for name, w in (('JOBs', win.jobsWidget), ('Single measurement', win.singleMeasurementWidget)):
        check(f'{name}: saving dir field has room', w.lineEdit_savingDir.minimumWidth() >= 250)
        check(f'{name}: experiment ID field has room', w.lineEdit_experimentID.minimumWidth() >= 160)
        w.lineEdit_savingDir.setText('/very/long/path/to/the/data/folder/of/this/lab/2026-10-20')
        check(f'{name}: tooltip shows the full path', w.lineEdit_savingDir.toolTip().endswith('2026-10-20'))

    win.recordTabWidget.setCurrentWidget(win.jobsWidget); app.processEvents()
    check('JOBs tab with long texts does not widen the panel', win.recordTabWidget.minimumSizeHint().width() < 700, win.recordTabWidget.minimumSizeHint().width())
    win.recordTabWidget.setCurrentWidget(win.singleMeasurementWidget); app.processEvents()
    check('Single measurement tab does not widen the panel', win.recordTabWidget.minimumSizeHint().width() < 700, win.recordTabWidget.minimumSizeHint().width())

    print('layout')
    widgets_all = win._panel_col1_widgets + win._panel_col2_widgets
    def report():
        win.show(); app.processEvents(); time.sleep(0.1); app.processEvents()
        return win.centralWidget.width(), win.controlPanel.width(), win.imageDisplayTabs.width()
    w1, p1, i1 = report()
    check('starts in one column', not win._panel_two_columns and win._panel_col2.count() == 1 and win.recordTabWidget.parent() is win.centralWidget)
    win.checkbox_two_columns.setChecked(True)
    w2, p2, i2 = report()
    check('two columns: panel wider', w2 > w1 + 100, (w1, w2))
    check('two columns: image display narrower', i2 < i1 - 100, (i1, i2))
    check('two columns: tabs sit right of the live control', win.recordTabWidget.geometry().x() > win.liveControlWidget.geometry().right() - 5,
          (win.recordTabWidget.geometry(), win.liveControlWidget.geometry()))
    check('two columns: all widgets visible and parented', all(w.parent() is win.centralWidget and w.isVisible() for w in widgets_all))
    check('preference saved', os.path.exists(pref) and 'true' in open(pref).read().lower())
    win.checkbox_two_columns.setChecked(False)
    w3, p3, i3 = report()
    check('back to one column: same width as before', abs(w3 - w1) <= 10, (w1, w3))
    check('back to one column: image display as before', abs(i3 - i1) <= 30, (i1, i3))
    check('back to one column: all widgets visible', all(w.isVisible() for w in widgets_all))
    win.checkbox_two_columns.setChecked(True); win.checkbox_two_columns.setChecked(False); win.checkbox_two_columns.setChecked(True)
    w4, _, _ = report()
    check('repeated toggling is stable', abs(w4 - w2) <= 10, (w2, w4))
    print('widths (central, panel, image): 1 col', (w1, p1, i1), '2 col', (w2, p2, i2), 'back', (w3, p3, i3))
    win.close()
finally:
    if os.path.exists(pref): os.remove(pref)
    if backup is not None: open(pref, 'w').write(backup)
print(f'{sum(results)}/{len(results)} checks passed')
sys.exit(0 if all(results) else 1)
