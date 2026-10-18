"""Offscreen checks of the main-window layout (divider between image display and controls can be dragged both ways),
the live-view font (fits the panel) and the confirmation window before an acquisition. Simulation, no hardware.

Run:  SQUID_E2E_OFFSCREEN=1 python tests/layout_offscreen.py
"""
import os
import sys
import time

SW = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(SW)
sys.path.insert(0, SW)
os.environ['QT_API'] = 'pyqt5'
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from unittest.mock import MagicMock
for m in ['napari', 'napari.utils', 'napari.utils.colormaps']:
    sys.modules[m] = MagicMock()
import control._def as d
d.USE_NAPARI_FOR_LIVE_VIEW = d.USE_NAPARI_FOR_MULTIPOINT = d.USE_NAPARI_FOR_TILED_DISPLAY = False
from qtpy.QtWidgets import QApplication, QDialog, QTableWidget, QMessageBox, QLabel
from qtpy.QtGui import QFontMetrics
import control.gui_hcs as gui
import control.acq_summary as acq

results = []


def record(name, ok, detail=''):
    results.append(bool(ok))
    print(('  PASS  ' if ok else '  FAIL  ') + name + (f'   [{detail}]' if detail and not ok else ''))


app = QApplication([])
app.setStyle('Fusion')
win = gui.OctopiGUI(is_simulation=True)
win.resize(1700, 1000)
win.show()


def pump(s=0.5):
    t = time.time()
    while time.time() - t < s:
        app.processEvents()
        time.sleep(0.01)


pump(1.0)
# --- layout
top = win.main_dockArea.topContainer
disp, ctrl = win.imageDisplayTabs.width(), win.controlPanel.width()
record('controls start at roughly their natural width', abs(ctrl - win._control_panel_width) < 60, f'{ctrl} vs {win._control_panel_width}')
s0 = top.sizes()
top.setSizes([s0[0] + 250, s0[1] - 250]); pump()
record('display can be made wider (controls narrower)', win.imageDisplayTabs.width() > disp + 200 and win.controlPanel.width() < ctrl - 200, f'{disp}->{win.imageDisplayTabs.width()}, {ctrl}->{win.controlPanel.width()}')
record('narrow controls scroll instead of being cut', win.controlPanel.horizontalScrollBar().maximum() > 0)
top.setSizes([s0[0] - 150, s0[1] + 150]); pump()
record('display can be made narrower (controls wider)', win.imageDisplayTabs.width() < disp - 100, f'{disp}->{win.imageDisplayTabs.width()}')
win.resize(1300, 800); pump()
record('main window can be resized', win.width() <= 1320, str(win.width()))

# --- live view fonts fit the panel
jw = win.jobsWidget
win.recordTabWidget.setCurrentWidget(jw); pump()
lv = jw.live
lv.begin(None, None, [])
lv.on_message(dict(type='state', state='waiting', until=100000.0, reason='interval', next_tp=1, t=0.0)); pump(0.3)
lab = lv.label_count
fm = QFontMetrics(lab.font())
w = fm.horizontalAdvance(lab.text()) if hasattr(fm, 'horizontalAdvance') else fm.width(lab.text())
record('countdown text fits the label', w <= lab.width(), f'{w} > {lab.width()}: {lab.text()}')
record('countdown font is not larger than 8 pt above the normal font', lab.font().pointSizeF() <= QApplication.font().pointSizeF() + 8.01, str(lab.font().pointSizeF()))
record('headline font is the normal GUI size', abs(lv.label_head.font().pointSizeF() - QApplication.font().pointSizeF()) < 0.6, str(lv.label_head.font().pointSizeF()))

# --- confirmation window
shown = []
orig_exec = QDialog.exec_


def fake_exec(self):
    tab = self.findChildren(QTableWidget)[0]
    shown.append(dict(title=self.windowTitle(), rows=[[tab.item(i, j).text() for j in range(tab.columnCount())] for i in range(tab.rowCount())],
                      labels=' | '.join(l.text() for l in self.findChildren(QLabel))))
    return QDialog.Accepted if fake_exec.answer else QDialog.Rejected


QDialog.exec_ = fake_exec
fake_exec.answer = False
mp = win.multiPointWidget2
mpc = win.multipointController
os.environ.pop('SQUID_SKIP_CONFIRM', None)
import tempfile
save_tmp = tempfile.mkdtemp()
mp.lineEdit_savingDir.setText(save_tmp); mp.base_path_is_set = True; mpc.set_base_path(save_tmp)
mp.lineEdit_experimentID.setText('layout_test')
started = []
mpc.run_acquisition = lambda *a, **k: started.append(1)
mp.list_configurations.selectAll()
mp.btn_startAcquisition.click(); pump(0.2)
record('multipoint: confirmation window shown', len(shown) == 1 and 'multipoint' in shown[0]['title'], str(shown))
record('multipoint: Cancel does not start and un-presses the button', not started and not mp.btn_startAcquisition.isChecked())
if shown:
    names = [r[0] for r in shown[0]['rows']]
    cfgs = {c.name: c for c in mpc.configurationManager.configurations}
    ok = all(r[1] == acq._fmt(cfgs[r[0]].exposure_time) and r[2] == acq._fmt(cfgs[r[0]].analog_gain) and r[3] == acq._fmt(cfgs[r[0]].illumination_intensity) for r in shown[0]['rows'])
    record('window lists exposure, gain, intensity of every selected channel', ok and len(names) == len(mp.list_configurations.selectedItems()), str(shown[0]['rows']))
    record('window names the trigger mode', 'rigger' in shown[0]['labels'], shown[0]['labels'])
fake_exec.answer = True
mp.btn_startAcquisition.click(); pump(0.2)
record('multipoint: OK starts the acquisition', len(started) == 1)
mp.btn_startAcquisition.setChecked(False); mp.setEnabled_all(True)

# job: cancel
import textwrap
work = tempfile.mkdtemp()
job = os.path.join(work, 'j.py')
open(job, 'w').write(textwrap.dedent('''
    def run_job(scope):
        scope.move_to(scope.points()[0])
        scope.capture(scope.channels[0])
'''))
mp.clear(); mp.add_location(); pump(0.2)
jw.lineEdit_job.setText(job); jw.lineEdit_savingDir.setText(work)
shown.clear(); fake_exec.answer = False
ran = []
mpc.run_job = lambda *a, **k: ran.append(1)
QMessageBox.warning = staticmethod(lambda *a, **k: None)
jw.btn_start.click(); pump(0.2)
record('job: confirmation window shown with the job channel', len(shown) == 1 and len(shown[0]['rows']) == 1 and 'job' in shown[0]['title'], str(shown))
record('job: Cancel does not start it', not ran and not jw.running and not jw.btn_start.isChecked())
fake_exec.answer = True
jw.btn_start.click(); pump(0.2)
record('job: OK starts it', len(ran) == 1 and jw.running)

QDialog.exec_ = orig_exec
print(f'\n{sum(results)} passed, {len(results) - sum(results)} failed')
sys.stdout.flush()
os._exit(0 if all(results) else 1)
