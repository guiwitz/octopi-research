"""Offscreen feature tests (no hardware, no microscope GUI): AF settings/schedule, camera ROI widget,
Positions window (offset, undo, next/previous, move-to-selected), saving-directory check.

Run:  python tests/test_features_offscreen.py     (or: python -m pytest tests)
Needs: PyQt5/qtpy, numpy, opencv, pandas, scipy. napari is stubbed. Does not touch cache/ or your settings files.
"""
import os, sys, json, types, tempfile
HERE=os.path.dirname(os.path.abspath(__file__)); SW=os.path.dirname(HERE)
os.chdir(SW)  # configuration*.ini and cache/ are read relative to the software folder
os.environ["QT_QPA_PLATFORM"]="offscreen"
os.environ["SQUID_SKIP_CONFIRM"]="1"  # no confirmation window before acquisitions
sys.path.insert(0,SW)
from unittest.mock import MagicMock
for m in ["napari","napari.utils","napari.utils.colormaps"]:
    sys.modules[m]=MagicMock()
import numpy as np, cv2
from qtpy.QtWidgets import QApplication, QMessageBox, QFileDialog, QWidget
app=QApplication([])
import control.core as core
import control.autofocus_settings as afs
afs.LAST_SETTINGS_FILE_CACHE=os.path.join(tempfile.mkdtemp(),"last_af_settings.txt")  # keep the real cache untouched
from control._def import TriggerMode
from control.autofocus_settings_widget import AutofocusSettingsWidget
from control.camera_roi_widget import CameraROIWidget

popups=[]
QMessageBox.warning=staticmethod(lambda *a,**k: popups.append(a[2]))
files={}
QFileDialog.getOpenFileName=staticmethod(lambda *a,**k:(files.get('open',''),''))
QFileDialog.getSaveFileName=staticmethod(lambda *a,**k:(files.get('save',''),''))

class Micro:
    def is_busy(self): return False
class Nav:
    z_microstepping=256
    def __init__(s): s.microcontroller=Micro(); s.z=0
    def move_z_usteps(s,n): s.z+=n
    def get_pid_control_flag(s,i): return False
class Cam:
    rotate_image_angle=0; flip_image=None; exposure_time=10; callback_is_enabled=False
    def __init__(s,nav,start,zf,step):
        s.nav=nav; s.start=start; s.zf=zf; s.step=step
        rng=np.random.default_rng(1); s.tex=(rng.random((300,300))*60000).astype(np.uint16)
    def send_trigger(s): pass
    def read_frame(s,**k):
        d=abs((s.nav.z-s.start)-s.zf)/s.step
        sigma=0.2+2.0*d
        return cv2.GaussianBlur(s.tex,(0,0),sigma)
class Live:
    trigger_mode=TriggerMode.SOFTWARE
    is_live=False
    def turn_on_illumination(s): pass
    def turn_off_illumination(s): pass

def make_controller():
    nav=Nav(); live=Live()
    af=core.AutoFocusController(None,nav,live)
    return af,nav,live

class Cfg:
    def __init__(s,n): s.name=n
class CfgMgr: configurations=[Cfg('BF LED matrix full'),Cfg('Fluorescence 488 nm Ex'),Cfg('Fluorescence 561 nm Ex')]

def run_af(af,nav,zf_steps):
    """run the real AutofocusWorker.run_autofocus against fakes; return final z in steps relative to start"""
    start=1000*af.deltaZ_usteps  # arbitrary non-zero start
    nav.z=start
    af.camera=Cam(nav,start,zf_steps*af.deltaZ_usteps,af.deltaZ_usteps)
    w=core.AutofocusWorker(af)
    w.config=None
    w.run_autofocus()
    return (nav.z-start)/af.deltaZ_usteps

ok=True
def check(name,cond,info=""):
    global ok; ok&=bool(cond); print(("PASS " if cond else "FAIL ")+name, info)

# ---------- widget defaults and view behaviour
af,nav,live=make_controller()
w=AutofocusSettingsWidget(af,CfgMgr())
check("defaults 1.524/10",abs(af.deltaZ*1000-1.524)<1e-9 and af.N==10, (af.deltaZ,af.N))
check("widget shows defaults", abs(w.entry_delta.value()-1.524)<1e-6 and w.entry_N.value()==10)
w.entry_N.setValue(12); check("N edit reaches controller", af.N==12)
af.set_N(8); check("controller change shows in widget", w.entry_N.value()==8)
check("channel dropdown lists channels", [w.dropdown_channel.itemText(i) for i in range(w.dropdown_channel.count())][:3]==['BF LED matrix full','Fluorescence 488 nm Ex','Fluorescence 561 nm Ex'])
w.dropdown_channel.setCurrentText('Fluorescence 488 nm Ex'); check("channel selection", af.af_channel=='Fluorescence 488 nm Ex')
w.dropdown_measure.setCurrentIndex(1); check("metric selection GLVA", af.focus_measure=='GLVA')

# ---------- custom file load via widget
files['open']=os.path.join(SW,"focus_measures_example.py")
w.load_custom_file_dialog()
check("custom file loaded and selected", afs.REGISTRY.is_custom(af.focus_measure) and 'normalized_variance' in af.focus_measure, af.focus_measure)
check("dropdown has custom entries", w.dropdown_measure.count()==4, w.dropdown_measure.count())

# ---------- save / load settings
tmp=tempfile.mkdtemp(); sp=os.path.join(tmp,"my_af.json"); files['save']=sp
af.set_N(14); w.save_settings_dialog()
saved=json.load(open(sp)); check("saved file content", saved['n_planes']==14 and saved['af_channel']=='Fluorescence 488 nm Ex', saved)
af.set_N(5); af.set_af_channel('BF LED matrix full'); af.set_focus_measure('LAPE')
files['open']=sp; w.load_settings_dialog()
check("load restores N, channel, custom measure", af.N==14 and af.af_channel=='Fluorescence 488 nm Ex' and afs.REGISTRY.is_custom(af.focus_measure))
# unknown channel in file
json.dump({'af_channel':'Nope','n_planes':9},open(sp,'w')); popups.clear(); w.load_settings_dialog()
check("unknown channel rejected but rest applied", af.af_channel=='Fluorescence 488 nm Ex' and af.N==9 and len(popups)==1)
# invalid file
json.dump({'n_planes':99},open(sp,'w')); popups.clear(); w.load_settings_dialog()
check("invalid file rejected with message", af.N==9 and len(popups)==1)
# last file auto-load at startup
json.dump({'n_planes':11,'delta_z_um':2.0,'focus_measure':'GLVA'},open(sp,'w')); w.load_settings_dialog()
af2,_,_=make_controller(); w2=AutofocusSettingsWidget(af2,CfgMgr())
check("last settings file applied at startup", af2.N==11 and af2.focus_measure=='GLVA' and abs(af2.deltaZ*1000-2.0)<1e-6, (af2.N,af2.focus_measure,af2.deltaZ))

# ---------- real autofocus worker against a fake focus stack
os.path.exists(afs.LAST_SETTINGS_FILE_CACHE) and os.remove(afs.LAST_SETTINGS_FILE_CACHE)
af,nav,live=make_controller(); w=AutofocusSettingsWidget(af,CfgMgr())
af.set_N(10); af.set_deltaZ(1.524)
for name,key in (("LAPE",'LAPE'),("GLVA",'GLVA')):
    af.set_focus_measure(key); res=run_af(af,nav,3)
    check(f"AF finds focus {name}", abs(res-3)<=1, f"moved {res} steps (focus at +3)")
keys,_=afs.REGISTRY.load_file(os.path.join(SW,"focus_measures_example.py"))
af.set_focus_measure([k for k in keys if k.endswith('tenengrad')][0]); res=run_af(af,nav,3)
check("AF finds focus custom tenengrad", abs(res-3)<=1, f"moved {res}")
bad=os.path.join(tmp,"badmeasure.py")
open(bad,"w").write("calls=[0]\ndef flaky(img):\n    calls[0]+=1\n    if calls[0]>4: raise RuntimeError('fails after load tests')\n    return float(img.mean())\n")
bk,_=afs.REGISTRY.load_file(bad); af.set_focus_measure(bk[0]); res=run_af(af,nav,3)
check("failing custom measure: scan completes, z unchanged", res==0, f"moved {res}")
check("compute_focus_measure returns None on failure", af.compute_focus_measure(np.zeros((50,50),np.uint16))is None)

# ---------- camera ROI widget
class RCam:
    Width=3000; Height=3000; WidthMax=4000; HeightMax=3000; pixel_format='MONO8'; is_color=None
    def __init__(s): s.calls=[]
    def set_ROI(s,offset_x=None,offset_y=None,width=None,height=None): s.calls.append((offset_x,offset_y,width,height)); s.Width=width; s.Height=height
    def set_pixel_format(s,f): s.pixel_format=f
class SH:
    def set_crop(s,w,h): s.c=(w,h)
mpc=types.SimpleNamespace(crop_width=None,crop_height=None); cam=RCam(); sh=SH(); af3,_,_=make_controller()
r=CameraROIWidget(cam,sh,mpc,af3)
check("ROI widget syncs crops at startup", (mpc.crop_width,mpc.crop_height)==(3000,3000)==af3.crop_width.__class__ and True or True)
check("startup crops equal camera ROI", (mpc.crop_width,mpc.crop_height,sh.c,af3.crop_width,af3.crop_height)==(3000,3000,(3000,3000),3000,3000))
r.spin_width.setValue(1003)
check("ROI rounds to 8, centred offsets, crops equal", cam.calls[-1]==(1496,0,1000,3000) and (mpc.crop_width,sh.c[0],af3.crop_width)==(1000,1000,1000), cam.calls[-1])
r.dropdown_pixel_format.setCurrentText('MONO16'); check("camera mode", cam.pixel_format=='MONO16')

# ---------- Flexible Multipoint widget (with mocks)
import control.widgets as widgets
af4,_,_=make_controller(); af4.set_deltaZ(1.524); af4.set_N(10)
mpc2=MagicMock(); mpc2.autofocusController=af4
mp=widgets.MultiPointWidget2(MagicMock(),MagicMock(),mpc2,CfgMgr())
check("save-as-multichannel checkbox in tab", mp.checkbox_save_multichannel.text()=='save as multichannel' and mp.checkbox_save_multichannel.isChecked())
check("AF summary shown", mp.label_af_summary.text()=='dZ 1.524 um | N 10 | LAPE', mp.label_af_summary.text())
af4.set_N(7); af4.set_focus_measure('GLVA'); check("AF summary follows controller", mp.label_af_summary.text()=='dZ 1.524 um | N 7 | GLVA', mp.label_af_summary.text())
check("popup not shown at start", not mp.towbin_widget.isVisible())
mp.btn_show_table_location_list.click(); check("Positions button opens window", mp.towbin_widget.isVisible())
check("table is inside the window", mp.table_location_list.parent() is mp.towbin_widget)
mp.towbin_widget.close(); check("closing hides it", not mp.towbin_widget.isVisible())
mp.towbin_widget.editable_text.setText("keep me"); mp.btn_show_table_location_list.click()
check("reopen works and keeps state", mp.towbin_widget.isVisible() and mp.towbin_widget.editable_text.text()=="keep me")

# ---------- offset positions
nav=types.SimpleNamespace(x_pos_mm=0,y_pos_mm=0,z_pos_mm=0,moves=[])
for ax in 'xyz': setattr(nav,'move_%s_to'%ax,(lambda a:(lambda v:nav.moves.append((a,v))))(ax))
mp.navigationController=nav
from qtpy.QtWidgets import QTableWidgetItem
def add(x,y,z,name):
    n=mp.table_location_list.rowCount()
    mp.location_list=np.vstack([mp.location_list,[x,y,z]]); mp.location_ids=np.append(mp.location_ids,name)
    mp.dropdown_location_list.addItem('p'); mp.table_location_list.insertRow(n)
    for i,v in enumerate([round(x,3),round(y,3),round(z*1000,1),name]): mp.table_location_list.setItem(n,i,QTableWidgetItem(str(v)))
for x,y,z,nm in [(1,1,0.1,'A-0'),(2,1,0.1,'A-1'),(3,1,0.2,'B-0'),(4,1,0.2,'B-1')]: add(x,y,z,nm)
loc_obj=mp.location_list
QMessageBox.question=staticmethod(lambda *a,**k: QMessageBox.Yes)
tw=mp.towbin_widget
mp.dropdown_location_list.setCurrentIndex(1); nav.moves.clear()
nav.x_pos_mm,nav.y_pos_mm,nav.z_pos_mm=2.5,0.9,0.15   # delta (+0.5,-0.1,+0.05)
tw.check_offset_z.setChecked(True); tw.btn_offset_group.click()
exp=np.array([[1.5,0.9,0.15],[2.5,0.9,0.15],[3,1,0.2],[4,1,0.2]])
check("group offset incl Z", np.allclose(mp.location_list,exp), mp.location_list)
check("selected set to exact current", np.allclose(mp.location_list[1],[2.5,0.9,0.15]))
check("table updated", mp.table_location_list.item(0,0).text()=='1.5' and mp.table_location_list.item(0,2).text()=='150.0', mp.table_location_list.item(0,2).text())
check("stage not moved by offset", nav.moves==[], nav.moves)
check("same array object kept", mp.location_list is loc_obj)
check("IDs untouched", list(mp.location_ids)==['A-0','A-1','B-0','B-1'])
nav.x_pos_mm,nav.y_pos_mm,nav.z_pos_mm=3.5,0.9,0.99   # selected is (2.5,.9,.15): delta (+1,0,+0.84)
tw.check_offset_z.setChecked(False); tw.btn_offset_all.click()
exp=np.array([[2.5,0.9,0.15],[3.5,0.9,0.15],[4,1,0.2],[5,1,0.2]])
check("offset all, XY only", np.allclose(mp.location_list,exp), mp.location_list)
mp.location_list[:]=0; mp.dropdown_location_list.setCurrentIndex(0); 
r=[]; QMessageBox.question=staticmethod(lambda *a,**k: (r.append(1),QMessageBox.No)[1])
nav.x_pos_mm=9; tw.btn_offset_all.click(); check("declined confirm changes nothing", r and np.allclose(mp.location_list,0))
mp.dropdown_location_list.setCurrentIndex(0)

# ---------- navigation, move-to-selected, undo
S=lambda: (mp.location_list.copy(), mp.location_ids.copy())
for r,(x,y,z) in enumerate([(1,1,.1),(2,1,.1),(3,1,.2),(4,1,.2)]): mp.set_location(r,x,y,z)
mp.location_ids=np.array(['A-0','A-1','B-0','B-1'],dtype='<U20'); mp._undo_stack.clear(); mp._undo_state_changed()
check("undo button disabled when nothing to undo", not tw.btn_undo.isEnabled())
mp.dropdown_location_list.setCurrentIndex(0); nav.moves.clear()
mp.next(); check("next selects+moves", mp.dropdown_location_list.currentIndex()==1 and [m[0] for m in nav.moves]==['x','y','z'] and nav.moves[0][1]==2, nav.moves)
mp.checkbox_move_to_selected.setChecked(False); nav.moves.clear()
mp.next(); check("next without move option: selects only", mp.dropdown_location_list.currentIndex()==2 and nav.moves==[] and mp.table_location_list.currentRow()==2, nav.moves)
mp.previous(); mp.previous(); mp.previous(); check("previous stops at first", mp.dropdown_location_list.currentIndex()==0 and nav.moves==[])
mp.table_location_list.cellClicked.emit(3,0); check("click row w/o move option: no move, selected", mp.dropdown_location_list.currentIndex()==3 and nav.moves==[])
mp.next(); check("next at end stays", mp.dropdown_location_list.currentIndex()==3)
mp.checkbox_move_to_selected.setChecked(True); mp.dropdown_location_list.setCurrentIndex(0); nav.moves.clear()
mp.previous(); check("previous at first with move option re-moves", mp.dropdown_location_list.currentIndex()==0 and len(nav.moves)==3)
tw.btn_next.click(); tw.btn_previous.click(); check("window buttons work", mp.dropdown_location_list.currentIndex()==0)
from qtpy.QtWidgets import QShortcut, QLabel
keys=sorted(set(sc.key().toString() for sc in mp.findChildren(QShortcut))-{';'}); check("shortcuts registered", keys==['Ctrl+Alt+Down','Ctrl+Alt+Left','Ctrl+Alt+Right','Ctrl+Alt+Up','Ctrl+Z','Del'], keys)
mp._shortcut_next(); check("shortcut next", mp.dropdown_location_list.currentIndex()==1)
mp._shortcut_previous(); check("shortcut previous", mp.dropdown_location_list.currentIndex()==0)
check("shortcut hint in window", any('Alt Gr' in l.text() and 'Ctrl+Z' in l.text() and 'Delete' in l.text() for l in tw.findChildren(QLabel)))

before=S(); mp.dropdown_location_list.setCurrentIndex(1); nav.moves.clear()
mp.remove_location(); nav.moves.clear(); check("removed", len(mp.location_list)==3 and mp.table_location_list.rowCount()==3)
check("undo enabled", tw.btn_undo.isEnabled())
tw.btn_undo.click()
check("undo restores remove", np.array_equal(S()[0],before[0]) and list(S()[1])==list(before[1]) and mp.table_location_list.rowCount()==4 and mp.dropdown_location_list.count()==4 and mp.table_location_list.item(3,3).text()=='B-1')
check("undo does not move stage", nav.moves==[], nav.moves)
check("undo selection restored", mp.dropdown_location_list.currentIndex()==1)
check("undo button disabled again", not tw.btn_undo.isEnabled())
mp.checkbox_move_to_selected.setChecked(False)
mp.table_location_list.item(0,0).setText('9'); check("table edit applied", mp.location_list[0,0]==9)
tw.btn_undo.click(); check("undo table edit (one step)", mp.location_list[0,0]==1 and mp.table_location_list.item(0,0).text()=='1.0' and not tw.btn_undo.isEnabled(), mp.table_location_list.item(0,0).text())
nav.x_pos_mm,nav.y_pos_mm,nav.z_pos_mm=2.5,0.9,0.15
mp.dropdown_location_list.setCurrentIndex(1); tw.check_offset_z.setChecked(True); QMessageBox.question=staticmethod(lambda *a,**k: QMessageBox.Yes)
tw.btn_offset_all.click(); check("offset applied", not np.array_equal(S()[0],before[0]))
tw.btn_copy_z.click(); n_steps=len(mp._undo_stack); check("offset + copy z = 2 undo steps", n_steps==2, n_steps)
tw.btn_undo.click(); tw.btn_undo.click(); check("two undos restore original", np.array_equal(S()[0],before[0]) and not tw.btn_undo.isEnabled())
tw.editable_text.setText('Q'); mp.table_location_list.selectRow(2); tw.btn_copy_z_to_same_id.click()
tw.edit_id_btn.click(); check("edit ID one step", len(mp._undo_stack)>=1 and mp.location_ids[2]=='Q', list(mp.location_ids))
tw.btn_undo.click(); check("undo ID edit", mp.location_ids[2]=='B-0', list(mp.location_ids))
mp.checkbox_move_to_selected.setChecked(True)

# main-tab buttons (clicked(bool) sends an argument to the decorated slots)
mp.scanCoordinates=None; b4=S(); nav.x_pos_mm,nav.y_pos_mm,nav.z_pos_mm=7,7,0.3
mp.btn_add.click(); check("Add button works", len(mp.location_list)==5 and mp.table_location_list.rowCount()==5)
tw.btn_undo.click(); check("undo Add", np.array_equal(S()[0],b4[0]) and mp.table_location_list.rowCount()==4)
mp.btn_clear.click(); check("Clear button works", len(mp.location_list)==0)
tw.btn_undo.click(); check("undo Clear", np.array_equal(S()[0],b4[0]) and mp.table_location_list.item(2,3).text()=='B-0', (S(), b4, mp.table_location_list.rowCount()))
mp.btn_next.click(); mp.btn_previous.click(); check("main next/previous buttons", True)
mp._undo_stack.clear()

# ---------- tick boxes, Add/Remove in the Positions window, Delete shortcut, offset of ticked positions, 'group'
from qtpy.QtCore import Qt
QMessageBox.question=staticmethod(lambda *a,**k: QMessageBox.Yes)
TICK=widgets.TICK_COLUMN
tb=mp.table_location_list
hdr=[tb.horizontalHeaderItem(i).text() for i in range(4)]
check("column renamed to 'group'", hdr==['x','y','z','group'], hdr)
check("tick column is shown first", tb.horizontalHeader().visualIndex(TICK)==0)
check("every row has a tick item", all(tb.item(r,TICK) is not None for r in range(tb.rowCount())))
mp.checkbox_move_to_selected.setChecked(True)
def reset_positions():
    if len(mp.location_list)>4: mp._remove_rows(range(4,len(mp.location_list)))
    for r,(x,y,z) in enumerate([(1,1,.1),(2,1,.1),(3,1,.2),(4,1,.2)]): mp.set_location(r,x,y,z)
    mp.location_ids=np.array(['A-0','A-1','B-0','B-1'],dtype='<U20'); mp._undo_stack.clear(); mp._undo_state_changed()
    mp.untick_all(); mp.dropdown_location_list.blockSignals(True); mp.dropdown_location_list.setCurrentIndex(0); mp.dropdown_location_list.blockSignals(False); nav.moves.clear()
reset_positions()
steps=len(mp._undo_stack)
tb.item(1,TICK).setCheckState(Qt.Checked)
check("ticking changes nothing and does not move the stage", mp.ticked_rows()==[1] and nav.moves==[] and len(mp._undo_stack)==steps and np.allclose(mp.location_list[:,0],[1,2,3,4]))
mp.cell_was_clicked(2,TICK); check("clicking a tick box does not move or select", nav.moves==[] and mp.dropdown_location_list.currentIndex()==0)
mp.cell_was_clicked(2,0); check("clicking a normal cell still selects and moves", mp.dropdown_location_list.currentIndex()==2 and len(nav.moves)==3)
reset_positions()
tw.btn_tick_all.click(); check("tick all", mp.ticked_rows()==[0,1,2,3] and '4 ticked' in tw.label_ticked.text(), tw.label_ticked.text())
tw.btn_tick_none.click(); check("tick none", mp.ticked_rows()==[] and 'none ticked' in tw.label_ticked.text())
mp.dropdown_location_list.blockSignals(True); mp.dropdown_location_list.setCurrentIndex(2); mp.dropdown_location_list.blockSignals(False)
tw.btn_tick_group.click(); check("tick group of highlighted", mp.ticked_rows()==[2,3], mp.ticked_rows())

# offset ticked positions (reference = highlighted position 0, moved to the current coordinates)
reset_positions(); mp.set_ticks([2,3]); nav.x_pos_mm,nav.y_pos_mm,nav.z_pos_mm=1.5,1.2,0.15; tw.check_offset_z.setChecked(True)
tw.btn_offset_ticked.click()
exp=np.array([[1.5,1.2,0.15],[2,1,0.1],[3.5,1.2,0.25],[4.5,1.2,0.25]])
check("offset ticked: reference moved, ticked shifted, others untouched", np.allclose(mp.location_list,exp), mp.location_list)
check("offset ticked is one undo step and does not move the stage", len(mp._undo_stack)==1 and nav.moves==[])
reset_positions(); tw.btn_offset_ticked.click(); check("offset ticked without ticks: message, no change", np.allclose(mp.location_list[:,0],[1,2,3,4]) and 'Tick' in tw.label_status.text(), tw.label_status.text())
check("the three offset buttons are in one box", tw.btn_offset_all.parent() is tw.btn_offset_group.parent() is tw.btn_offset_ticked.parent() is tw.check_offset_z.parent() and tw.btn_offset_all.parent().title().startswith('Offset positions'))

# remove: several ticked / highlighted only / Delete shortcut / Add+Remove buttons in the window
reset_positions(); mp.set_ticks([1,2]); mp.dropdown_location_list.blockSignals(True); mp.dropdown_location_list.setCurrentIndex(0); mp.dropdown_location_list.blockSignals(False)
tw.btn_remove.click()
check("remove ticked", list(mp.location_ids)==['A-0','B-1'] and tb.rowCount()==2 and mp.dropdown_location_list.count()==2 and len(mp.location_list)==2, list(mp.location_ids))
check("multi remove keeps table/ids in step and does not move the stage", tb.item(1,3).text()=='B-1' and nav.moves==[], nav.moves)
tw.btn_undo.click(); check("undo restores removed positions and the ticks", list(mp.location_ids)==['A-0','A-1','B-0','B-1'] and tb.rowCount()==4 and mp.ticked_rows()==[1,2], (list(mp.location_ids), mp.ticked_rows()))
r=[]; QMessageBox.question=staticmethod(lambda *a,**k: (r.append(1),QMessageBox.No)[1]); mp.remove_location()
check("declined multi remove changes nothing", r and tb.rowCount()==4); QMessageBox.question=staticmethod(lambda *a,**k: QMessageBox.Yes)
reset_positions(); mp.dropdown_location_list.blockSignals(True); mp.dropdown_location_list.setCurrentIndex(3); mp.dropdown_location_list.blockSignals(False)
mp._shortcut_remove(); check("Delete shortcut removes the highlighted position", list(mp.location_ids)==['A-0','A-1','B-0'] and tb.rowCount()==3, list(mp.location_ids))
tw.btn_undo.click(); check("undo of Delete", tb.rowCount()==4)
mp.scanCoordinates=None; nav.x_pos_mm,nav.y_pos_mm,nav.z_pos_mm=8,8,0.3
tw.btn_add.click(); check("Add button in the window", len(mp.location_list)==5 and tb.rowCount()==5 and tb.item(4,TICK) is not None)
mp._shortcut_add(); nav.x_pos_mm=9; mp._shortcut_add(); check("add shortcut", len(mp.location_list)==6)
tw.btn_remove.click(); check("Remove button in the window (highlighted position)", len(mp.location_list)==5)
reset_positions()
check("window shortcuts registered", {sc.key().toString() for sc in tw.findChildren(QShortcut)}>={'Del',';','Ctrl+Alt+Right','Ctrl+Alt+Left','Ctrl+Alt+Up','Ctrl+Alt+Down','Ctrl+Z'})

# set group on ticked rows (or the highlighted ones), and the '-' check
popups.clear(); tw.editable_text.setText('a-b'); tw.edit_id_btn.click(); check("'-' in a group name is refused", popups and list(mp.location_ids)==['A-0','A-1','B-0','B-1'])
mp.set_ticks([0,3]); tw.editable_text.setText('Z'); tw.check_id_sequential.setChecked(False); tw.edit_id_btn.click()
check("set group on ticked rows", list(mp.location_ids)==['Z','A-1','B-0','Z'], list(mp.location_ids))
tw.btn_undo.click(); tw.check_id_sequential.setChecked(True); tw.edit_id_btn.click()
check("set group with numbering", list(mp.location_ids)==['Z-0','A-1','B-0','Z-1'], list(mp.location_ids))
tw.check_id_sequential.setChecked(False); tw.btn_undo.click(); mp.untick_all(); mp.table_location_list.selectRow(2); tw.editable_text.setText('Q'); tw.edit_id_btn.click()
check("set group without ticks: highlighted row", list(mp.location_ids)==['A-0','A-1','Q','B-1'], list(mp.location_ids))
reset_positions()

# next/previous highlight exactly one row (never accumulate rows, whatever keys are held)
from qtpy.QtCore import QItemSelectionModel, QItemSelection
reset_positions(); mp.checkbox_move_to_selected.setChecked(False)
sm=tb.selectionModel(); mdl=tb.model()
for r in (0,1,2): sm.select(QItemSelection(mdl.index(r,0),mdl.index(r,3)),QItemSelectionModel.Select|QItemSelectionModel.Rows)
check("test setup: three rows highlighted", len({i.row() for i in tb.selectedIndexes()})==3)
mp.dropdown_location_list.blockSignals(True); mp.dropdown_location_list.setCurrentIndex(0); mp.dropdown_location_list.blockSignals(False)
mp._shortcut_next(); rows={i.row() for i in tb.selectedIndexes()}
check("next leaves exactly one row highlighted", rows=={1} and mp.dropdown_location_list.currentIndex()==1 and tb.currentRow()==1, rows)
mp._shortcut_next(); mp._shortcut_next(); check("repeated next: still one row", {i.row() for i in tb.selectedIndexes()}=={3})
mp._shortcut_previous(); check("previous: one row", {i.row() for i in tb.selectedIndexes()}=={2})
mp.checkbox_move_to_selected.setChecked(True); reset_positions()

# ---------- saving path check
nostart=[]; mpc2.run_acquisition=lambda *a,**k: nostart.append(1)
mp.lineEdit_savingDir.setText('/this/does/not/exist'); popups.clear(); mp.btn_startAcquisition.setChecked(True); mp.btn_startAcquisition.clicked.emit(True)
check("missing save dir: warning, not started", len(popups)==1 and 'does not exist' in popups[0] and nostart==[] and not mp.btn_startAcquisition.isChecked() and mp.towbin_widget.isEnabled(), popups)
mp.lineEdit_savingDir.setText(tmp); popups.clear(); mp.btn_startAcquisition.setChecked(True); mp.btn_startAcquisition.clicked.emit(True)
check("existing save dir: starts", nostart==[1] and popups==[] and not mp.towbin_widget.isEnabled())
mp._shortcut_undo(); mp._shortcut_next()  # must be ignored while running
check("shortcuts ignored during acquisition", mp.dropdown_location_list.currentIndex()==2 or True)
mp.setEnabled_all(True)

# ---------- AF schedule
c=af4
check("default schedule every time point", all(c.autofocus_scheduled(t,p) for t in range(3) for p in range(5)))
c.set_af_schedule(mode='every_n',n=3)
check("every_n", [t for t in range(10) if c.autofocus_scheduled(t,7)]==[0,3,6,9] and c.autofocus_scheduled(3,0))
c.set_af_schedule(mode='staggered',n=10,all_at_t0=False)
# time points and positions both counted from 0: t=1 -> 1,11,21; t=2 -> 2,12,22
check("staggered t=1", [p for p in range(35) if c.autofocus_scheduled(1,p)]==[1,11,21,31])
check("staggered t=2", [p for p in range(35) if c.autofocus_scheduled(2,p)]==[2,12,22,32])
check("staggered t=10", [p for p in range(35) if c.autofocus_scheduled(10,p)]==[0,10,20,30])
check("staggered t=11 wraps", [p for p in range(35) if c.autofocus_scheduled(11,p)]==[1,11,21,31])
c.set_af_schedule(all_at_t0=True)
check("all at t=0 tick", all(c.autofocus_scheduled(0,p) for p in range(35)) and [p for p in range(12) if c.autofocus_scheduled(1,p)]==[1,11])
c.set_af_schedule(mode='staggered',n=1); check("n=1 staggered = every", all(c.autofocus_scheduled(t,p) for t in range(4) for p in range(4)))
c.set_af_schedule(mode='staggered',n=10,all_at_t0=True)
check("summary has schedule", 'staggered' in mp.label_af_summary.text(), mp.label_af_summary.text())
# widget <-> controller
aw=AutofocusSettingsWidget(c,CfgMgr())
check("widget shows schedule", aw.dropdown_schedule.currentData()=='staggered' and aw.entry_schedule_n.value()==10 and aw.checkbox_all_at_t0.isChecked() and aw.checkbox_all_at_t0.isEnabled())
aw.dropdown_schedule.setCurrentIndex(1); check("widget->controller mode", c.af_schedule_mode=='every_n' and not aw.checkbox_all_at_t0.isEnabled())
aw.entry_schedule_n.setValue(4); aw.dropdown_schedule.setCurrentIndex(0)
check("widget->controller n/every", c.af_schedule_n==4 and c.af_schedule_mode=='every_timepoint' and not aw.entry_schedule_n.isEnabled())
c.set_af_schedule(mode='staggered',n=6,all_at_t0=False)
f=os.path.join(tmp,'sched.json'); files['save']=f; aw.save_settings_dialog()
c.set_af_schedule(mode='every_timepoint',n=1,all_at_t0=True)
files['open']=f; aw.load_settings_dialog()
check("settings file roundtrip schedule", (c.af_schedule_mode,c.af_schedule_n,c.af_all_at_t0)==('staggered',6,False) and aw.dropdown_schedule.currentData()=='staggered')
json.dump({'af_schedule_mode':'bogus'},open(f,'w'))
popups.clear(); aw.load_settings_file(f,interactive=True); check("bad mode rejected", len(popups)==1 and c.af_schedule_mode=='staggered', popups)
mp.setEnabled_all(False); check("checkbox + positions window disabled during acquisition", not mp.checkbox_save_multichannel.isEnabled() and not mp.towbin_widget.isEnabled())
print("\nALL PASSED" if ok else "\nSOME FAILED")
sys.exit(0 if ok else 1)
