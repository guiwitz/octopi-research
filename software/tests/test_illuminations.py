"""Illuminations tab: ConfigurationManager (duplicate, delete, add from the permanent list, edit; xml written) and the table widget.
Run:  QT_QPA_PLATFORM=offscreen python -m pytest tests/test_illuminations.py"""
import os
import shutil
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ['SQUID_SKIP_CONFIRM'] = '1'
SW = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SW)
os.chdir(SW)
from qtpy.QtCore import Qt  # noqa: E402
from qtpy.QtWidgets import QApplication  # noqa: E402
import control.core as core  # noqa: E402
from control.illumination_table_widget import IlluminationTableWidget, check_name, check_values, parse_number  # noqa: E402

app = QApplication.instance() or QApplication([])


EXTRA = 'Fluorescence 638 nm Ex'     # a non-core illumination (the core ones cannot be deleted)


def manager(tmp_path):
    fn = str(tmp_path / 'channel_configurations.xml')
    shutil.copy(os.path.join(SW, 'channel_configurations.xml'), fn)
    from lxml import etree as ET
    tree = ET.parse(fn)
    root = tree.getroot()
    if not any(m.get('Name') == EXTRA for m in root.iter('mode')):
        first = next(root.iter('mode'))
        m = ET.SubElement(root, 'mode')
        for k, v in first.attrib.items():
            m.set(k, v)
        m.set('ID', str(max(int(x.get('ID')) for x in root.iter('mode') if x is not m) + 1))
        m.set('Name', EXTRA)
        m.set('IlluminationSource', '13')
        tree.write(fn, encoding='utf-8', xml_declaration=True, pretty_print=True)
    return core.ConfigurationManager(fn)


def extra(cm):
    return next(c for c in cm.configurations if c.name == EXTRA)


def reread(cm):
    return core.ConfigurationManager(cm.config_filename)


def test_duplicate_adds_a_line_with_new_id_and_name(tmp_path):
    cm = manager(tmp_path)
    n = len(cm.configurations)
    got = []
    cm.configurations_changed.connect(lambda: got.append(1))
    src = next(c for c in cm.configurations if '488' in c.name)
    d = cm.duplicate_configuration(src.id)
    assert len(cm.configurations) == n + 1 and got == [1]
    assert d.name == src.name + ' (2)' and d.id != src.id and d.illumination_source == src.illumination_source
    d2 = cm.duplicate_configuration(src.id)
    assert d2.name == src.name + ' (3)'
    cm2 = reread(cm)
    assert [c.name for c in cm2.configurations] == [c.name for c in cm.configurations]
    assert len({c.id for c in cm2.configurations}) == len(cm2.configurations)


def test_set_values_written_and_ids_exact(tmp_path):
    cm = manager(tmp_path)
    src = cm.configurations[0]
    d = cm.duplicate_configuration(src.id)
    cm.set_values(d.id, name='my channel', exposure=33, gain=4.5, intensity=12)
    c2 = next(c for c in reread(cm).configurations if c.id == d.id)
    assert (c2.name, c2.exposure_time, c2.analog_gain, c2.illumination_intensity) == ('my channel', 33.0, 4.5, 12.0)
    # the original is untouched; update_configuration of an ID that is a substring of another ID hits the right one
    assert next(c for c in reread(cm).configurations if c.id == src.id).name == src.name
    try:
        cm.set_values(d.id, name=src.name)
        raise AssertionError('duplicate name must be refused')
    except ValueError:
        pass


def test_core_illuminations_cannot_be_deleted_others_can(tmp_path):
    cm = manager(tmp_path)
    for c in list(cm.configurations):
        if cm.is_core(c):
            try:
                cm.delete_configuration(c.id)
                raise AssertionError('core illumination deleted: ' + c.name)
            except ValueError as e:
                assert 'core' in str(e)
            try:
                cm.set_values(c.id, name=c.name + ' x')
                raise AssertionError('core illumination renamed')
            except ValueError:
                pass
    # non-core: the one from the file (from an earlier session) and new ones
    d = cm.duplicate_configuration(cm.configurations[0].id)
    e = extra(cm)
    cm.delete_configuration(d.id)
    cm.delete_configuration(e.id)
    names = [c.name for c in reread(cm).configurations]          # also after a restart
    assert EXTRA not in names and d.name not in names
    assert all(n in names for n in cm.CORE_NAMES if n in [c.name for c in cm.configurations])


def test_templates_are_permanent_and_add_from_template(tmp_path):
    cm = manager(tmp_path)
    names = cm.template_names()
    assert names == [c.name for c in cm.configurations] and os.path.isfile(cm.templates_filename())
    victim = extra(cm)
    cm.delete_configuration(victim.id)
    assert cm.template_names() == names          # not changed by the deletion
    c = cm.add_from_template(victim.name)
    assert c.name == victim.name and cm.get_configuration(c.id) is c
    c = cm.add_from_template(victim.name)
    assert c.name == victim.name + ' (2)'


def test_checks():
    assert parse_number('12,5') == 12.5
    assert check_name('', []) and check_name('a/b', []) and check_name('x', ['x']) and check_name('x (2)', ['x']) is None
    assert check_values(exposure=0) and check_values(intensity=120) and check_values(exposure=10, gain=2, intensity=50) is None


def test_table_widget(tmp_path):
    cm = manager(tmp_path)
    w = IlluminationTableWidget(cm)
    assert w.table.rowCount() == len(cm.configurations)
    # built-in names are fixed, numbers editable
    assert not (w.table.item(0, 0).flags() & Qt.ItemIsEditable) and (w.table.item(0, 1).flags() & Qt.ItemIsEditable)
    w.table.item(0, 1).setText('45,5')
    assert cm.configurations[0].exposure_time == 45.5 and reread(cm).configurations[0].exposure_time == 45.5
    w.table.item(0, 2).setText('abc')                      # refused, table shows the old value again
    assert w.table.item(0, 2).text() == '%g' % cm.configurations[0].analog_gain
    w.table.item(0, 3).setText('500')                      # intensity out of range
    assert cm.configurations[0].illumination_intensity <= 100
    w.table.setCurrentCell(1, 0)
    w.duplicate()
    assert w.table.rowCount() == len(cm.configurations) and w.table.currentRow() == len(cm.configurations) - 1
    r = w.table.currentRow()
    assert (w.table.item(r, 0).flags() & Qt.ItemIsEditable)
    w.table.item(r, 0).setText('bright 488')
    assert cm.configurations[-1].name == 'bright 488'
    n = len(cm.configurations)
    w.delete()
    assert len(cm.configurations) == n - 1
    w.combo_template.setCurrentIndex(0)
    w.add_from_template()
    assert len(cm.configurations) == n


def test_table_refuses_while_acquisition_runs(tmp_path):
    cm = manager(tmp_path)

    class Busy:
        def acquisition_running(self):
            return True
    w = IlluminationTableWidget(cm, Busy())
    old = cm.configurations[0].exposure_time
    w.table.item(0, 1).setText('99')
    assert cm.configurations[0].exposure_time == old
    w.table.setCurrentCell(0, 0)
    n = len(cm.configurations)
    w.duplicate()
    w.delete()
    assert len(cm.configurations) == n


def test_live_control_and_multipoint_lists_follow(tmp_path):
    from unittest.mock import MagicMock
    try:
        import napari  # noqa: F401
    except ImportError:
        for m in ['napari', 'napari.utils', 'napari.utils.colormaps']:
            sys.modules[m] = MagicMock()
    import control.widgets as widgets
    cm = manager(tmp_path)
    live = MagicMock()
    live.camera.EXPOSURE_TIME_MS_MIN, live.camera.EXPOSURE_TIME_MS_MAX = 0.1, 10000
    live.camera.GAIN_MIN, live.camera.GAIN_MAX, live.camera.GAIN_STEP = 0, 30, 0.1
    lcw = widgets.LiveControlWidget(MagicMock(), live, cm)
    names0 = [lcw.dropdown_modeSelection.itemText(i) for i in range(lcw.dropdown_modeSelection.count())]
    assert names0 == [c.name for c in cm.configurations]
    d = cm.duplicate_configuration(cm.configurations[1].id)
    assert lcw.dropdown_modeSelection.findText(d.name) >= 0
    cm.set_values(lcw.currentConfiguration.id, exposure=77)           # edit of the current channel shows in the live control box
    assert lcw.entry_exposureTime.value() == 77
    lcw.update_microscope_mode_by_name(d.name)
    cm.delete_configuration(d.id)                                     # deleting the current channel selects another one
    assert lcw.currentConfiguration in cm.configurations
    from qtpy.QtWidgets import QListWidget, QAbstractItemView
    for cls in (widgets.MultiPointWidget, widgets.MultiPointWidget2):     # the list of the multipoint tabs (their refresh method on a stand-in, the full widgets need a whole controller)
        class Holder:
            configurationManager = cm
            emitted = []
            def emit_selected_channels(self_):
                self_.emitted.append([i.text() for i in self_.list_configurations.selectedItems()])
        h = Holder()
        h.list_configurations = QListWidget()
        h.list_configurations.setSelectionMode(QAbstractItemView.MultiSelection)
        h.list_configurations.addItems([c.name for c in cm.configurations])
        h.list_configurations.item(1).setSelected(True)
        keep = h.list_configurations.item(1).text()
        cm.duplicate_configuration(cm.configurations[0].id)
        cls.refresh_channel_list(h)
        assert h.list_configurations.count() == len(cm.configurations)
        assert [i.text() for i in h.list_configurations.selectedItems()] == [keep] and h.emitted[-1] == [keep]


def test_channel_order_widget(tmp_path):
    from control.channel_order_widget import ChannelOrderWidget
    cm = manager(tmp_path)
    w = ChannelOrderWidget(cm)
    names = [c.name for c in cm.configurations]
    got = []
    w.itemSelectionChanged.connect(lambda: got.append(w.used_names()))
    assert w.selectedItems() == [] and w.table_avail.rowCount() == len(names) and w.table_used.rowCount() == 0
    # push over in a chosen order: 3rd, then 1st
    w.table_avail.selectRow(2)
    w.add_selected()
    w.table_avail.selectRow(0)
    w.add_selected()
    assert w.used_names() == [names[2], names[0]] and w.table_avail.rowCount() == len(names) - 2
    # move the first used one down, then up again
    w.table_used.selectRow(0)
    w.move(1)
    assert w.used_names() == [names[0], names[2]]
    w.table_used.selectRow(1)
    w.move(1)                                           # already last: nothing
    assert w.used_names() == [names[0], names[2]]
    w.move(-1)
    assert w.used_names() == [names[2], names[0]]
    # back
    w.table_used.selectRow(0)
    w.remove_selected()
    assert w.used_names() == [names[0]] and w.table_avail.rowCount() == len(names) - 1
    w.add_all()
    assert w.used_names() == [names[0]] + [n for n in names if n != names[0]]
    w.remove_all()
    assert w.used_names() == [] and got
    # list interface used by the acquisition code and the tests
    for it in w.findItems(names[1]):
        it.setSelected(True)
    w.item(0).setSelected(True)
    assert w.used_names() == [names[1], names[0]] and w.count() == len(names)
    w.clearSelection()
    assert w.used_names() == []
    # follows the Illuminations tab: order kept, deleted channels disappear, renamed ones keep their place
    k = names.index(EXTRA)
    w.item(k).setSelected(True); w.item(0).setSelected(True)
    d = cm.duplicate_configuration(cm.configurations[0].id)
    cm.set_values(d.id, name='dup renamed')
    w.refresh_from_manager()
    assert w.used_names() == [names[k], names[0]] and w.table_avail.rowCount() == len(cm.configurations) - 2
    cm.delete_configuration(extra(cm).id)
    w.refresh_from_manager()
    assert w.used_names() == [names[0]]


def test_illumination_sets_save_load_delete_import_export(tmp_path):
    from control.illumination_sets_dialog import IlluminationSetsDialog
    from control.channel_order_widget import ChannelOrderWidget
    cm = manager(tmp_path)
    orig = [(c.name, c.exposure_time) for c in cm.configurations]
    order = ChannelOrderWidget(cm)
    order.item([c.name for c in cm.configurations].index(EXTRA)).setSelected(True); order.item(0).setSelected(True)
    used_before = order.used_names()
    dlg = IlluminationSetsDialog(cm)
    assert dlg.list_sets.count() == 0
    dlg.save_current('day 1')
    assert cm.list_sets() == ['day 1'] and dlg.preview.rowCount() == len(orig)
    # change the current illuminations, save as a second set
    d = cm.duplicate_configuration(cm.configurations[0].id)
    cm.set_values(d.id, name='extra', exposure=55)
    cm.delete_configuration(extra(cm).id)
    dlg.save_current('day 2')
    assert cm.list_sets() == ['day 1', 'day 2']
    # load day 1: everything as at the start (also on disk), the used order kept by name
    dlg.list_sets.setCurrentRow(0)
    dlg.load_selected()
    assert [(c.name, c.exposure_time) for c in cm.configurations] == orig
    assert [(c.name, c.exposure_time) for c in reread(cm).configurations] == orig
    order.refresh_from_manager()
    assert order.used_names() == used_before
    # load day 2
    dlg.list_sets.setCurrentRow(1)
    dlg.load_selected()
    assert 'extra' in [c.name for c in cm.configurations] and EXTRA not in [c.name for c in cm.configurations]
    order.refresh_from_manager()
    assert order.used_names() == [n for n in used_before if n != EXTRA]
    # export / import / delete
    out = str(tmp_path / 'exported.xml')
    dlg.export_selected(out)
    dlg.import_file(out, 'imported')
    assert 'imported' in cm.list_sets()
    dlg.list_sets.setCurrentRow(0)
    dlg.delete_selected()
    assert 'day 1' not in cm.list_sets()
    # broken file is refused, nothing changes
    bad = tmp_path / 'bad.xml'
    bad.write_text('<modes><mode Name="x"/></modes>')
    n = len(cm.list_sets())
    dlg.import_file(str(bad), 'bad')
    assert len(cm.list_sets()) == n and 'lacks' in dlg.label_status.text()
    try:
        cm.safe_set_name('a/b')
        raise AssertionError('bad name accepted')
    except ValueError:
        pass


def test_sets_not_loaded_while_acquisition_runs(tmp_path):
    from control.illumination_sets_dialog import IlluminationSetsDialog
    cm = manager(tmp_path)
    dlg = IlluminationSetsDialog(cm, busy=lambda: True)
    dlg.save_current('s')
    cm.delete_configuration(extra(cm).id)
    n = len(cm.configurations)
    dlg.load_selected()
    assert len(cm.configurations) == n


def test_loading_a_set_without_core_illuminations_keeps_them(tmp_path):
    cm = manager(tmp_path)
    from lxml import etree as ET
    path = str(tmp_path / 'only_extra.xml')
    root = ET.Element('modes')
    m = ET.SubElement(root, 'mode')
    for k, v in next(x for x in cm.config_xml_tree_root.iter('mode') if x.get('Name') == EXTRA).attrib.items():
        m.set(k, v)
    m.set('ID', '1')       # same ID as a current core one: it gets a free ID
    ET.ElementTree(root).write(path, encoding='utf-8', xml_declaration=True)
    core_before = [c.name for c in cm.configurations if cm.is_core(c)]
    cm.load_file(path)
    names = [c.name for c in cm.configurations]
    assert EXTRA in names and all(n in names for n in core_before)
    assert len({c.id for c in cm.configurations}) == len(cm.configurations)
