"""Two tables for the channel selection of the Flexible Multipoint tab: LEFT the available illuminations, RIGHT the illuminations used in
the acquisition, in the order in which they are acquired (top = first). Buttons push illuminations over (> / >>) or back (< / <<) and move
the selected one up or down; double click on a line moves it to the other table. An illumination is used at most once.

The widget offers the parts of the QListWidget interface that the acquisition code and the tests use (selectedItems() in acquisition order,
itemSelectionChanged, count, item, findItems, clearSelection, selectAll), so it replaces the old list without other changes.
The exposure column is read only (change the values in the Illuminations tab or the live control box).
"""
from qtpy.QtCore import Qt, Signal, QSize
from qtpy.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QGridLayout, QTableWidget, QTableWidgetItem, QPushButton, QLabel, QAbstractItemView,
                            QHeaderView)


class _Table(QTableWidget):
    """small preferred size (the default 256 x 192 widens the control panel); the tables get the space that is there"""
    def sizeHint(self):
        return QSize(100, 120)

    def minimumSizeHint(self):
        return QSize(60, 70)


class _Item:
    """what selectedItems() / item() / findItems() return: like a QListWidgetItem (text(), setSelected())"""

    def __init__(self, widget, conf_id):
        self._w, self._id = widget, conf_id

    def text(self):
        c = self._w.cm.get_configuration(self._id)
        return c.name if c is not None else ''

    def setSelected(self, on):
        self._w._set_used(self._id, bool(on))

    def isSelected(self):
        return self._id in self._w._used


class ChannelOrderWidget(QWidget):
    itemSelectionChanged = Signal()

    def __init__(self, configurationManager, parent=None):
        super().__init__(parent)
        self.cm = configurationManager
        self._used = []                      # ids of the used illuminations, in acquisition order
        self._enabled_buttons = []

        def make_table():
            t = _Table(0, 2)
            t.setHorizontalHeaderLabels(['Available', 'ms'])
            t.verticalHeader().setVisible(False)
            t.setEditTriggers(QAbstractItemView.NoEditTriggers)
            t.setSelectionBehavior(QAbstractItemView.SelectRows)
            t.setSelectionMode(QAbstractItemView.ExtendedSelection)
            t.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
            t.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
            t.setMinimumHeight(90)
            t.setMaximumHeight(190)
            return t
        self.table_avail = make_table()
        self.table_used = make_table()
        self.table_used.setHorizontalHeaderLabels(['Used (in order)', 'ms'])

        def button(text, tip, slot):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.setFixedWidth(30)
            b.clicked.connect(slot)
            self._enabled_buttons.append(b)
            return b
        mid = QVBoxLayout()
        mid.addStretch()
        mid.addWidget(button('>', 'Use the selected illuminations (appended at the end of the order)', self.add_selected))
        mid.addWidget(button('>>', 'Use all illuminations', self.add_all))
        mid.addWidget(button('<', 'Do not use the selected illuminations', self.remove_selected))
        mid.addWidget(button('<<', 'Use none', self.remove_all))
        mid.addWidget(button('\u25b2', 'Acquire the selected used illumination earlier (up)', lambda: self.move(-1)))
        mid.addWidget(button('\u25bc', 'Acquire the selected used illumination later (down)', lambda: self.move(1)))
        mid.addStretch()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)
        lay.addWidget(self.table_avail, 1)
        lay.addLayout(mid)
        lay.addWidget(self.table_used, 1)
        self.table_used.setToolTip('Used illuminations: the acquisition takes them from top to bottom')
        self.table_avail.setToolTip('Available illuminations: select and push to the right with > (or double click)')

        self.table_avail.cellDoubleClicked.connect(lambda r, c: self._add_ids([self._id_at(self.table_avail, r)]))
        self.table_used.cellDoubleClicked.connect(lambda r, c: self._remove_ids([self._id_at(self.table_used, r)]))
        self._render()

    # ---- state -> tables
    def _id_at(self, table, row):
        return table.item(row, 0).data(Qt.UserRole)

    def _selected_ids(self, table):
        rows = sorted({i.row() for i in table.selectedItems()})
        return [self._id_at(table, r) for r in rows]

    def _render(self, select_used=()):
        confs = {str(c.id): c for c in self.cm.configurations}
        self._used = [i for i in self._used if str(i) in confs]
        used = set(map(str, self._used))
        for table, ids in ((self.table_avail, [c.id for c in self.cm.configurations if str(c.id) not in used]), (self.table_used, list(self._used))):
            table.blockSignals(True)
            table.setRowCount(len(ids))
            for r, i in enumerate(ids):
                c = confs[str(i)]
                a = QTableWidgetItem(c.name)
                a.setData(Qt.UserRole, c.id)
                a.setToolTip(f'{c.name}\nexposure {c.exposure_time:g} ms, gain {c.analog_gain:g}, intensity {c.illumination_intensity:g} %')
                b = QTableWidgetItem('%g' % c.exposure_time)
                b.setTextAlignment(Qt.AlignCenter)
                table.setItem(r, 0, a)
                table.setItem(r, 1, b)
            table.blockSignals(False)
        for r in range(self.table_used.rowCount()):
            if self._id_at(self.table_used, r) in select_used:
                self.table_used.selectRow(r)

    def _changed(self, select_used=()):
        self._render(select_used)
        self.itemSelectionChanged.emit()

    def showEvent(self, e):
        super().showEvent(e)
        self._render()           # exposure values may have been changed elsewhere

    def refresh_from_manager(self):
        """channel list changed (Illuminations tab): keep the used ones that still exist, in their order"""
        self._changed()

    # ---- moving
    def _add_ids(self, ids):
        new = [i for i in ids if i is not None and i not in self._used]
        if new:
            self._used += new
            self._changed(new)

    def _remove_ids(self, ids):
        if any(i in self._used for i in ids):
            self._used = [i for i in self._used if i not in ids]
            self._changed()

    def add_selected(self):
        self._add_ids(self._selected_ids(self.table_avail))

    def add_all(self):
        self._add_ids([c.id for c in self.cm.configurations])

    def remove_selected(self):
        self._remove_ids(self._selected_ids(self.table_used))

    def remove_all(self):
        self._remove_ids(list(self._used))

    def move(self, step):
        sel = self._selected_ids(self.table_used)
        if not sel:
            return
        order = list(self._used)
        idx = sorted(order.index(i) for i in sel)
        if step < 0:
            if idx[0] == 0:
                return
            for k in idx:
                order[k - 1], order[k] = order[k], order[k - 1]
        else:
            if idx[-1] == len(order) - 1:
                return
            for k in reversed(idx):
                order[k + 1], order[k] = order[k], order[k + 1]
        self._used = order
        self._changed(sel)

    # ---- QListWidget-like interface
    def selectedItems(self):
        """the used illuminations in acquisition order"""
        return [_Item(self, i) for i in self._used]

    def count(self):
        return len(self.cm.configurations)

    def item(self, i):
        return _Item(self, self.cm.configurations[i].id)

    def findItems(self, text, flags=None):
        return [_Item(self, c.id) for c in self.cm.configurations if c.name == text]

    def clearSelection(self):
        self._remove_ids(list(self._used))

    def selectAll(self):
        self.add_all()

    def _set_used(self, conf_id, on):
        (self._add_ids if on else self._remove_ids)([conf_id])

    def setEnabled(self, enabled):
        super().setEnabled(enabled)

    def used_names(self):
        return [i.text() for i in self.selectedItems()]
