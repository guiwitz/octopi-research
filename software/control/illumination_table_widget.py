"""Illuminations tab: the channels (illuminations) of channel_configurations.xml as a table (name, exposure, gain, intensity) that can be edited;
a channel can be duplicated (copy with the same illumination source and filter, then modify it), deleted, or a new one can be added from the
permanent template list (channel_templates.xml, a copy of the channels that were available when this tab was first used).

Every edit is written to channel_configurations.xml at once and ConfigurationManager.configurations_changed is emitted, so the live control, the
multipoint lists, the JOBs / Single measurement tabs and the autofocus channel list follow. Editing is refused while an acquisition runs.
The name of the channels that come from the template list cannot be changed (the software uses parts of these names, e.g. 'Fluorescence 488 nm Ex');
duplicates and new channels get a name like '<name> (2)' that can be edited (not: / \\ : * ? " < > |).
"""
import os
from qtpy.QtCore import Qt
from qtpy.QtWidgets import (QFrame, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem, QPushButton, QComboBox, QLabel, QMessageBox,
                            QAbstractItemView, QHeaderView)

COLS = ['Name', 'Exposure (ms)', 'Gain', 'Intensity (%)']
BAD_CHARS = '/\\:*?"<>|'


def parse_number(text):
    """'12,5' / '12.5' -> 12.5; ValueError if not a number"""
    return float(str(text).strip().replace(',', '.'))


def check_values(exposure=None, gain=None, intensity=None, exp_range=(0.01, 60000.0), gain_range=(0.0, 100.0), int_range=(0.0, 100.0)):
    """-> error text or None"""
    for label, v, (lo, hi) in (('Exposure', exposure, exp_range), ('Gain', gain, gain_range), ('Intensity', intensity, int_range)):
        if v is not None and not (lo <= v <= hi):
            return f'{label} must be between {lo:g} and {hi:g}.'
    return None


def check_name(name, other_names):
    name = name.strip()
    if not name:
        return 'The name must not be empty.'
    if any(ch in name for ch in BAD_CHARS):
        return 'The name must not contain any of  ' + ' '.join(BAD_CHARS)
    if name in other_names:
        return 'A channel with this name exists already.'
    return None


class IlluminationTableWidget(QFrame):

    def __init__(self, configurationManager, multipointController=None, camera=None, parent=None):
        super().__init__(parent)
        self.cm = configurationManager
        self.mpc = multipointController
        self.camera = camera
        self._filling = False
        self.setFrameStyle(QFrame.Panel | QFrame.Raised)

        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3):
            self.table.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeToContents)

        self.btn_duplicate = QPushButton('Duplicate selected')
        self.btn_duplicate.setToolTip('New line in the channel list with the same illumination source and filter; change its name, exposure, gain or intensity afterwards.')
        self.btn_delete = QPushButton('Delete selected')
        self.combo_template = QComboBox()
        self.btn_add = QPushButton('Add from list')
        self.btn_add.setToolTip('New channel from the permanent list of channels (channel_templates.xml next to channel_configurations.xml).')
        self.label_status = QLabel('')
        self.label_status.setWordWrap(True)
        info = QLabel('Changes are saved at once in channel_configurations.xml. The name of the channels from the permanent list is fixed; '
                      'duplicates and new channels can be renamed. Not possible while an acquisition is running.')
        info.setWordWrap(True)
        info.setStyleSheet('font-size: 8pt; color: gray;')

        row = QHBoxLayout()               # two short rows: one long row made the left column of the main window wide
        row.addWidget(self.btn_duplicate)
        row.addWidget(self.btn_delete)
        row.addStretch()
        row2 = QHBoxLayout()
        row2.addWidget(QLabel('New from the list:'))
        row2.addWidget(self.combo_template, 1)
        row2.addWidget(self.btn_add)
        self.combo_template.setMinimumContentsLength(14)
        self.combo_template.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        lay = QVBoxLayout(self)
        lay.addWidget(self.table)
        lay.addLayout(row)
        lay.addLayout(row2)
        lay.addWidget(self.label_status)
        lay.addWidget(info)

        self.btn_duplicate.clicked.connect(self.duplicate)
        self.btn_delete.clicked.connect(self.delete)
        self.btn_add.clicked.connect(self.add_from_template)
        self.table.itemChanged.connect(self._item_changed)
        self.cm.configurations_changed.connect(self.refresh)
        self._template_names = []
        self.refresh()

    # --- helpers
    def _busy(self):
        try:
            return bool(self.mpc is not None and self.mpc.acquisition_running())
        except Exception:
            return False

    def _say(self, text):
        self.label_status.setText(text)

    def _warn(self, text):
        self._say(text)
        if not os.environ.get('SQUID_SKIP_CONFIRM'):
            QMessageBox.warning(self, 'Illuminations', text)

    def _ranges(self):
        c = self.camera
        exp = (float(getattr(c, 'EXPOSURE_TIME_MS_MIN', 0.01) or 0.01), float(getattr(c, 'EXPOSURE_TIME_MS_MAX', 60000.0) or 60000.0))
        gain = (float(getattr(c, 'GAIN_MIN', 0.0)), float(getattr(c, 'GAIN_MAX', 100.0)))
        return exp, gain

    def _selected_id(self):
        r = self.table.currentRow()
        if r < 0:
            return None
        return self.table.item(r, 0).data(Qt.UserRole)

    def _row_of(self, conf_id):
        for r in range(self.table.rowCount()):
            if str(self.table.item(r, 0).data(Qt.UserRole)) == str(conf_id):
                return r
        return -1

    # --- table <- manager
    def refresh(self, *_):
        self._filling = True
        try:
            keep = self._selected_id()
            try:
                self._template_names = self.cm.template_names()
            except Exception:
                self._template_names = []
            current = self.combo_template.currentText()
            self.combo_template.clear()
            self.combo_template.addItems(self._template_names)
            if current in self._template_names:
                self.combo_template.setCurrentText(current)
            confs = self.cm.configurations
            self.table.setRowCount(len(confs))
            for r, c in enumerate(confs):
                vals = [c.name, '%g' % c.exposure_time, '%g' % c.analog_gain, '%g' % c.illumination_intensity]
                fixed_name = c.name in self._template_names
                for col, v in enumerate(vals):
                    it = QTableWidgetItem(v)
                    if col:
                        it.setTextAlignment(Qt.AlignCenter)
                    flags = it.flags() | Qt.ItemIsEditable
                    if col == 0:
                        it.setData(Qt.UserRole, c.id)
                        if fixed_name:
                            flags &= ~Qt.ItemIsEditable
                            it.setToolTip('The name of channels from the permanent list is fixed. Duplicate it to get a channel with a name you can change.')
                    it.setFlags(flags)
                    self.table.setItem(r, col, it)
            if keep is not None and self._row_of(keep) >= 0:
                self.table.setCurrentCell(self._row_of(keep), 0)
        finally:
            self._filling = False

    def showEvent(self, e):
        super().showEvent(e)
        self.refresh()          # values may have been changed in the live control box

    # --- table -> manager
    def _item_changed(self, item):
        if self._filling:
            return
        r, col = item.row(), item.column()
        conf_id = self.table.item(r, 0).data(Qt.UserRole)
        conf = self.cm.get_configuration(conf_id)
        if conf is None:
            return
        if self._busy():
            self.refresh()
            return self._warn('An acquisition is running: the illuminations cannot be changed now.')
        try:
            if col == 0:
                err = check_name(item.text(), [c.name for c in self.cm.configurations if c is not conf])
                if err:
                    self.refresh()
                    return self._warn(err)
                self.cm.set_values(conf_id, name=item.text().strip())
            else:
                try:
                    v = parse_number(item.text())
                except ValueError:
                    self.refresh()
                    return self._warn('"' + item.text() + '" is not a number.')
                exp_range, gain_range = self._ranges()
                kw = {1: 'exposure', 2: 'gain', 3: 'intensity'}[col]
                err = check_values(exp_range=exp_range, gain_range=gain_range, **{kw: v})
                if err:
                    self.refresh()
                    return self._warn(err)
                self.cm.set_values(conf_id, **{kw: v})
            self._say('Saved.')
        except Exception as e:
            self.refresh()
            self._warn('Not saved: ' + str(e))

    # --- buttons
    def duplicate(self):
        if self._busy():
            return self._warn('An acquisition is running: the illuminations cannot be changed now.')
        cid = self._selected_id()
        if cid is None:
            return self._say('Select a line first.')
        c = self.cm.duplicate_configuration(cid)
        self._select_new(c)
        self._say(f'Duplicated: "{c.name}". Change its name and values in the table.')

    def add_from_template(self):
        if self._busy():
            return self._warn('An acquisition is running: the illuminations cannot be changed now.')
        name = self.combo_template.currentText()
        if not name:
            return self._say('The list of channels is empty.')
        c = self.cm.add_from_template(name)
        self._select_new(c)
        self._say(f'Added "{c.name}".')

    def delete(self):
        if self._busy():
            return self._warn('An acquisition is running: the illuminations cannot be changed now.')
        cid = self._selected_id()
        c = self.cm.get_configuration(cid) if cid is not None else None
        if c is None:
            return self._say('Select a line first.')
        if len(self.cm.configurations) <= 1:
            return self._warn('The last channel cannot be deleted.')
        if not os.environ.get('SQUID_SKIP_CONFIRM'):
            if QMessageBox.question(self, 'Delete channel', f'Delete the channel "{c.name}" from the channel list?\n\n'
                                    'It can be added again from the permanent list if it is in there. Jobs or the autofocus setting that use this name will not find it.',
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return
        name = c.name
        self.cm.delete_configuration(cid)
        self._say(f'Deleted "{name}".')

    def _select_new(self, conf):
        r = self._row_of(conf.id)
        if r >= 0:
            self.table.setCurrentCell(r, 0)
            self.table.scrollToItem(self.table.item(r, 0))
