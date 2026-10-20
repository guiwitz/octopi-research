"""Save / load window for illumination sets (opened from the Illuminations tab).

A set is a complete channel list (all illuminations with name, exposure, gain, intensity, light source, filter ...), stored as one xml file
in the folder illumination_sets/ next to channel_configurations.xml (same format as that file, so a set can also be copied by hand).
  Save current as...   the current illuminations as a new set (or overwrite an existing one, asks first)
  Load selected        REPLACES all current illuminations by the set (asks first; save the current ones before if needed)
  Delete selected      deletes the set file (asks first); the current illuminations are not changed
  Import file... / Export selected...   xml files elsewhere (e.g. from another microscope - check light sources and filters)
The right side shows the illuminations of the selected set. Not possible while an acquisition runs.
"""
import os
from qtpy.QtCore import Qt
from qtpy.QtWidgets import (QDialog, QHBoxLayout, QVBoxLayout, QListWidget, QTableWidget, QTableWidgetItem, QPushButton, QLabel, QMessageBox,
                            QInputDialog, QFileDialog, QAbstractItemView, QHeaderView)


def _confirm(parent, title, text):
    if os.environ.get('SQUID_SKIP_CONFIRM'):
        return True
    return QMessageBox.question(parent, title, text, QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes


class IlluminationSetsDialog(QDialog):

    def __init__(self, configurationManager, parent=None, busy=None):
        super().__init__(parent)
        self.cm = configurationManager
        self.busy = busy or (lambda: False)
        self.setWindowTitle('Save / load illumination sets')
        self.resize(720, 380)

        self.list_sets = QListWidget()
        self.list_sets.setSelectionMode(QAbstractItemView.SingleSelection)
        self.preview = QTableWidget(0, 4)
        self.preview.setHorizontalHeaderLabels(['Illumination', 'Exposure (ms)', 'Gain', 'Intensity (%)'])
        self.preview.verticalHeader().setVisible(False)
        self.preview.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.preview.setSelectionMode(QAbstractItemView.NoSelection)
        self.preview.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3):
            self.preview.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeToContents)

        self.btn_save = QPushButton('Save current as...')
        self.btn_load = QPushButton('Load selected')
        self.btn_load.setStyleSheet('background-color: #C2C2FF')
        self.btn_delete = QPushButton('Delete selected')
        self.btn_import = QPushButton('Import file...')
        self.btn_export = QPushButton('Export selected...')
        self.btn_close = QPushButton('Close')
        self.label_status = QLabel('')
        self.label_status.setWordWrap(True)
        info = QLabel('Sets are stored in ' + self.cm.sets_dir() + '.  "Load" replaces ALL current illuminations.')
        info.setWordWrap(True)
        info.setStyleSheet('font-size: 8pt; color: gray;')

        left = QVBoxLayout()
        left.addWidget(QLabel('Saved sets'))
        left.addWidget(self.list_sets)
        right = QVBoxLayout()
        self.label_preview = QLabel('Illuminations of the selected set')
        right.addWidget(self.label_preview)
        right.addWidget(self.preview)
        top = QHBoxLayout()
        top.addLayout(left, 1)
        top.addLayout(right, 2)
        buttons = QHBoxLayout()
        for b in (self.btn_save, self.btn_load, self.btn_delete, self.btn_import, self.btn_export):
            buttons.addWidget(b)
        buttons.addStretch()
        buttons.addWidget(self.btn_close)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addLayout(buttons)
        lay.addWidget(self.label_status)
        lay.addWidget(info)

        self.list_sets.currentTextChanged.connect(self._show_preview)
        self.btn_save.clicked.connect(lambda: self.save_current())
        self.btn_load.clicked.connect(lambda: self.load_selected())
        self.btn_delete.clicked.connect(lambda: self.delete_selected())
        self.btn_import.clicked.connect(lambda: self.import_file())
        self.btn_export.clicked.connect(lambda: self.export_selected())
        self.btn_close.clicked.connect(self.accept)
        self.refresh()

    # --- helpers
    def _say(self, text):
        self.label_status.setText(text)

    def _warn(self, text):
        self._say(text)
        if not os.environ.get('SQUID_SKIP_CONFIRM'):
            QMessageBox.warning(self, 'Illumination sets', text)

    def _busy(self):
        try:
            if self.busy():
                self._warn('An acquisition is running: illumination sets cannot be loaded now.')
                return True
        except Exception:
            pass
        return False

    def selected(self):
        it = self.list_sets.currentItem()
        return it.text() if it is not None else None

    def refresh(self, select=None):
        keep = select or self.selected()
        self.list_sets.blockSignals(True)
        self.list_sets.clear()
        self.list_sets.addItems(self.cm.list_sets())
        self.list_sets.blockSignals(False)
        items = self.list_sets.findItems(keep, Qt.MatchExactly) if keep else []
        if items:
            self.list_sets.setCurrentItem(items[0])
        elif self.list_sets.count():
            self.list_sets.setCurrentRow(0)
        self._show_preview(self.selected())
        has = self.selected() is not None
        for b in (self.btn_load, self.btn_delete, self.btn_export):
            b.setEnabled(has)

    def _show_preview(self, name):
        self.preview.setRowCount(0)
        if not name:
            self.label_preview.setText('Illuminations of the selected set (no sets saved yet)')
            return
        try:
            modes = self.cm.read_set_file(self.cm.set_path(name))
        except Exception as e:
            self.label_preview.setText(f'"{name}" cannot be read: {e}')
            return
        self.label_preview.setText(f'Illuminations of "{name}" ({len(modes)})')
        self.preview.setRowCount(len(modes))
        for r, m in enumerate(modes):
            for c, key in enumerate(('Name', 'ExposureTime', 'AnalogGain', 'IlluminationIntensity')):
                it = QTableWidgetItem(str(m.get(key, '')))
                if c:
                    it.setTextAlignment(Qt.AlignCenter)
                self.preview.setItem(r, c, it)

    # --- actions (the name / path arguments are for the tests; without them a dialog asks)
    def save_current(self, name=None):
        if name is None:
            name, ok = QInputDialog.getText(self, 'Save illuminations', 'Name of the set:', text=self.selected() or '')
            if not ok:
                return
        try:
            name = self.cm.safe_set_name(name)
        except ValueError as e:
            return self._warn(str(e))
        overwrite = False
        if name in self.cm.list_sets():
            if not _confirm(self, 'Overwrite set', f'A set "{name}" exists already. Overwrite it with the current illuminations?'):
                return
            overwrite = True
        self.cm.save_set(name, overwrite=overwrite)
        self.refresh(select=name)
        self._say(f'Saved the current {len(self.cm.configurations)} illuminations as "{name}".')

    def load_selected(self):
        name = self.selected()
        if name is None or self._busy():
            return
        if not _confirm(self, 'Load illuminations', f'Replace ALL current illuminations by the set "{name}"?\n\n'
                        'The current ones are lost unless they were saved as a set.'):
            return
        try:
            confs = self.cm.load_set(name)
        except Exception as e:
            return self._warn('Not loaded: ' + str(e))
        self._say(f'Loaded "{name}": {len(confs)} illuminations.')

    def delete_selected(self):
        name = self.selected()
        if name is None:
            return
        if not _confirm(self, 'Delete set', f'Delete the saved set "{name}"?\n\nThe current illuminations are not changed.'):
            return
        try:
            self.cm.delete_set(name)
        except Exception as e:
            return self._warn('Not deleted: ' + str(e))
        self.refresh()
        self._say(f'Deleted the set "{name}".')

    def import_file(self, path=None, name=None):
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, 'Import illumination set', '', 'Channel lists (*.xml)')
            if not path:
                return
        if name is None:
            default = os.path.splitext(os.path.basename(path))[0]
            if os.environ.get('SQUID_SKIP_CONFIRM'):
                name = default
            else:
                name, ok = QInputDialog.getText(self, 'Import illumination set', 'Name of the set:', text=default)
                if not ok:
                    return
        try:
            name = self.cm.safe_set_name(name)
            overwrite = False
            if name in self.cm.list_sets():
                if not _confirm(self, 'Overwrite set', f'A set "{name}" exists already. Overwrite it?'):
                    return
                overwrite = True
            modes = self.cm.import_file_to_set(path, name, overwrite=overwrite)
        except Exception as e:
            return self._warn('Not imported: ' + str(e))
        self.refresh(select=name)
        self._say(f'Imported "{name}" ({len(modes)} illuminations). Check light sources and filters if it comes from another microscope; "Load selected" to use it.')

    def export_selected(self, path=None):
        name = self.selected()
        if name is None:
            return
        if path is None:
            path, _ = QFileDialog.getSaveFileName(self, 'Export illumination set', name + '.xml', 'Channel lists (*.xml)')
            if not path:
                return
        try:
            import shutil
            shutil.copyfile(self.cm.set_path(name), path)
        except Exception as e:
            return self._warn('Not exported: ' + str(e))
        self._say(f'Exported "{name}" to {path}.')
