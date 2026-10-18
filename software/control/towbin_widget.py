"""Positions window of the Flexible Multipoint tab.

Opened with the 'Positions...' button next to the location list. It contains the position table
(owned by the Flexible Multipoint widget, only shown here) and the buttons to edit it. Closing the
window only hides it (nothing is lost) and it can be opened again; it has no settings of its own.
"""
from qtpy.QtWidgets import (QWidget, QPushButton, QVBoxLayout, QLabel, QGroupBox, QHBoxLayout,
                            QLineEdit, QCheckBox, QGridLayout, QMessageBox, QTableWidgetItem)

from qtpy.QtCore import Qt


def _undo_step(method):
    """One undo step per call of a position-changing button."""
    import functools
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):  # args: the 'checked' value of clicked(bool) is not needed
        with self.parent.undo_group():
            return method(self)
    return wrapper


class TowbinWidget(QWidget):
    def __init__(self, parent=None):
        super(TowbinWidget, self).__init__(parent)

        self.setWindowFlags(Qt.Window)
        self.setWindowTitle('Positions')
        self.resize(520, 600)
        self.move(100, 100)

        self.parent = parent  # the Flexible Multipoint widget that owns the location list

        self.layout = QVBoxLayout()
        self.setLayout(self.layout)

        # the position table lives here (reparented from the Flexible Multipoint widget)
        self.layout.addWidget(self.parent.table_location_list, 1)

        from control.widgets import POSITION_SHORTCUTS, POSITION_SHORTCUTS_ALT
        grid_nav = QGridLayout()
        self.layout.addLayout(grid_nav)
        self.btn_add = QPushButton("Add")
        self.btn_add.setToolTip("Add the current stage position to the list")
        self.btn_add.clicked.connect(self.parent.add_location)
        grid_nav.addWidget(self.btn_add, 0, 0)
        self.btn_remove = QPushButton("Remove")
        self.btn_remove.setToolTip("Remove the ticked positions, or (no ticks) the highlighted position")
        self.btn_remove.clicked.connect(self.parent.remove_location)
        grid_nav.addWidget(self.btn_remove, 0, 1)
        self.btn_previous = QPushButton("Previous position")
        self.btn_previous.clicked.connect(self.parent.previous)
        grid_nav.addWidget(self.btn_previous, 0, 2)
        self.btn_next = QPushButton("Next position")
        self.btn_next.clicked.connect(self.parent.next)
        grid_nav.addWidget(self.btn_next, 0, 3)
        self.btn_undo = QPushButton("Undo")
        self.btn_undo.setToolTip("Undo the last change of the position list (add, remove, edit, offset, ...)")
        self.btn_undo.clicked.connect(self.parent.undo_positions)
        grid_nav.addWidget(self.btn_undo, 0, 4)
        grid_nav.addWidget(self.parent.checkbox_move_to_selected, 1, 0, 1, 5)
        label_keys = QLabel("Shortcuts: Alt Gr + \u2190/\u2191 previous | Alt Gr + \u2192/\u2193 next (= Ctrl+Alt + arrow) | "
                            f"{POSITION_SHORTCUTS['add']} add | {POSITION_SHORTCUTS['remove']} remove | {POSITION_SHORTCUTS['undo']} undo")
        label_keys.setStyleSheet('font-size: 8pt; color: gray;')
        grid_nav.addWidget(label_keys, 2, 0, 1, 5)
        self.update_undo_button()

        # tick boxes: select several positions
        row_ticks = QHBoxLayout()
        self.layout.addLayout(row_ticks)
        row_ticks.addWidget(QLabel("Tick:"))
        self.btn_tick_all = QPushButton("all")
        self.btn_tick_all.clicked.connect(lambda *_: self.parent.tick_all())
        row_ticks.addWidget(self.btn_tick_all)
        self.btn_tick_none = QPushButton("none")
        self.btn_tick_none.clicked.connect(lambda *_: self.parent.untick_all())
        row_ticks.addWidget(self.btn_tick_none)
        self.btn_tick_group = QPushButton("group of highlighted")
        self.btn_tick_group.clicked.connect(lambda *_: self.parent.tick_group_of_current())
        row_ticks.addWidget(self.btn_tick_group)
        self.label_ticked = QLabel("")
        self.label_ticked.setStyleSheet('font-size: 8pt; color: gray;')
        row_ticks.addWidget(self.label_ticked, 1)
        table = self.parent.table_location_list
        table.cellChanged.connect(lambda *_: self.update_tick_label())
        table.model().rowsInserted.connect(lambda *_: self.update_tick_label())
        table.model().rowsRemoved.connect(lambda *_: self.update_tick_label())
        self.update_tick_label()

        grid_edit_points_id = QGridLayout()
        self.layout.addLayout(grid_edit_points_id)

        self.editable_text = QLineEdit()
        grid_edit_points_id.addWidget(self.editable_text)

        self.editable_text.setPlaceholderText("group name, e.g. A (without '-')")
        self.edit_id_btn = QPushButton("Set group(s)")
        self.edit_id_btn.setToolTip("Set the group of the ticked positions (or, without ticks, of the highlighted rows)")
        self.edit_id_btn.clicked.connect(self.edit_id_selected_location)
        grid_edit_points_id.addWidget(self.edit_id_btn)

        self.check_id_sequential = QCheckBox("add number (A-0, A-1, ...)")
        self.check_id_sequential.setToolTip("Number the positions within the group: A-0, A-1, A-2, ... in table order")
        grid_edit_points_id.addWidget(self.check_id_sequential)

        grid_shift_points = QGridLayout()
        self.layout.addLayout(grid_shift_points)

        self.btn_update_position = QPushButton("Update Position")
        grid_shift_points.addWidget(self.btn_update_position)
        self.btn_update_position.clicked.connect(self.update_position)

        self.btn_copy_z = QPushButton("Copy Z to all")
        grid_shift_points.addWidget(self.btn_copy_z)
        self.btn_copy_z.clicked.connect(self.copy_z_to_all)
        self.btn_copy_z_to_same_id = QPushButton("Copy Z to group")
        self.btn_copy_z_to_same_id.clicked.connect(self.copy_z_to_same_id)
        grid_shift_points.addWidget(self.btn_copy_z_to_same_id)

        # the three offset buttons and 'also shift Z' belong together: one framed box
        box_offset = QGroupBox("Offset positions")
        box_offset.setStyleSheet("QGroupBox { border: 1px solid #707070; border-radius: 4px; margin-top: 10px; } "
                                 "QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }")
        grid_offset = QGridLayout(box_offset)
        self.layout.addWidget(box_offset)
        self.btn_offset_all = QPushButton("Offset all positions")
        self.btn_offset_all.setToolTip("Move the highlighted position to the current stage coordinates and shift all "
                                       "other positions by the same amount (current - saved).")
        self.btn_offset_all.clicked.connect(self.offset_all)
        grid_offset.addWidget(self.btn_offset_all, 0, 0)
        self.btn_offset_group = QPushButton("Offset positions of the same group")
        self.btn_offset_group.setToolTip("Same, but only for positions in the same group as the highlighted one.")
        self.btn_offset_group.clicked.connect(self.offset_group)
        grid_offset.addWidget(self.btn_offset_group, 0, 1)
        self.btn_offset_ticked = QPushButton("Offset ticked positions")
        self.btn_offset_ticked.setToolTip("Same, but only for the ticked positions (the highlighted reference position is moved as well).")
        self.btn_offset_ticked.clicked.connect(self.offset_ticked)
        grid_offset.addWidget(self.btn_offset_ticked, 0, 2)
        self.check_offset_z = QCheckBox("also shift Z")
        self.check_offset_z.setChecked(True)
        grid_offset.addWidget(self.check_offset_z, 1, 0)
        label_offset = QLabel("Reference = the highlighted position: it is moved to the current stage coordinates, the others are shifted by the same amount.")
        label_offset.setStyleSheet('font-size: 8pt; color: gray;')
        label_offset.setWordWrap(True)
        grid_offset.addWidget(label_offset, 1, 1, 1, 2)
        self.label_status = QLabel("")
        self.label_status.setStyleSheet('font-size: 8pt; color: gray;')
        self.label_status.setWordWrap(True)
        self.layout.addWidget(self.label_status)

        self.parent.destroyed.connect(self.close_widget)

    def update_undo_button(self):
        self.btn_undo.setEnabled(self.parent.can_undo())

    def update_tick_label(self):
        n = len(self.parent.ticked_rows())
        self.label_ticked.setText(f"{n} ticked" if n else "none ticked: actions use the highlighted position")

    def close_widget(self):
        self.close()

    @_undo_step
    def edit_id_selected_location(self):
        """Set the group of the ticked positions (or the highlighted rows if none is ticked). If the checkbox
        is checked, the positions are numbered: A-0, A-1, ... in table order."""

        if '-' in self.editable_text.text():
            QMessageBox.warning(self, 'Group name', "Please avoid the character '-' in the group name "
                                "(the group is the part before the first '-').")
            return
        for ind, row in enumerate(sorted(self.parent.rows_to_act_on())):
            item = self.parent.table_location_list.item(row, 3)
            if self.check_id_sequential.isChecked():
                item.setText(f'{self.editable_text.text()}-{ind}')
            else:
                item.setText(self.editable_text.text())

    @_undo_step
    def update_position(self):
        """Update the position of the selected location."""
        
        index = self.parent.dropdown_location_list.currentIndex()

        x_old = self.parent.location_list[index][0]
        y_old = self.parent.location_list[index][1]
        self.parent.navigationViewer.deregister_fov_to_image(x_old,y_old)

        x = self.parent.navigationController.x_pos_mm
        y = self.parent.navigationController.y_pos_mm
        z = self.parent.navigationController.z_pos_mm

        
        location_str = 'x: ' + str(round(x,3)) + ' mm, y: ' + str(round(y,3)) + ' mm, z: ' + str(round(1000*z,1)) + ' um'
        self.parent.dropdown_location_list.setItemText(index, location_str)
        
        
        self.parent.location_list[index] = [x, y, z]
        self.parent.table_location_list.setItem(index, 0, QTableWidgetItem(str(round(x,3))))
        self.parent.table_location_list.setItem(index, 1, QTableWidgetItem(str(round(y,3))))
        self.parent.table_location_list.setItem(index, 2, QTableWidgetItem(str(round(1000*z,1))))
        self.parent.navigationViewer.register_fov_to_image(x,y)

    @_undo_step
    def copy_z_to_all(self):
        """Copy the Z value of the selected location to all the locations in the
        table."""
        
        selected_items = self.parent.table_location_list.selectedItems()
        row = selected_items[0].row()
        z_value = self.parent.table_location_list.item(row, 2).text()
        for row in range(self.parent.table_location_list.rowCount()):
            item = self.parent.table_location_list.item(row, 2)
            item.setText(z_value)

    @_undo_step
    def copy_z_to_same_id(self):
        """Copy the Z value of the selected location to all the locations with the
        same group ID."""

        selected_items = self.parent.table_location_list.selectedItems()
        row = selected_items[0].row()
        z_value = self.parent.table_location_list.item(row, 2).text()
        id = self.parent.table_location_list.item(row, 3).text()
        id = id.split('-')[0]
        for row in range(self.parent.table_location_list.rowCount()):
            if self.parent.table_location_list.item(row, 3).text().split('-')[0] == id:
                item = self.parent.table_location_list.item(row, 2)
                item.setText(z_value)

    # ---- offsetting --------------------------------------------------------------------
    def _selected_row(self):
        """Row of the selected location (the dropdown follows the table selection); None if empty."""
        if self.parent.location_list.shape[0] == 0:
            return None
        index = self.parent.dropdown_location_list.currentIndex()
        return index if index >= 0 else None

    def offset_all(self):
        self._offset(group_only=False)

    def offset_group(self):
        self._offset(group_only=True)

    def offset_ticked(self):
        self._offset(ticked_only=True)

    def _offset(self, group_only=False, ticked_only=False):
        """Set the selected position to the current stage coordinates and shift the other
        positions (all, or those with the same group ID) by the same delta (current - saved)."""
        p = self.parent
        row = self._selected_row()
        if row is None:
            self._status("No position selected.")
            return
        nav = p.navigationController
        current = [nav.x_pos_mm, nav.y_pos_mm, nav.z_pos_mm]
        saved = [float(v) for v in p.location_list[row]]
        use_z = self.check_offset_z.isChecked()
        delta = [current[0] - saved[0], current[1] - saved[1], (current[2] - saved[2]) if use_z else 0.0]

        group = str(p.location_ids[row]).split('-')[0]
        if ticked_only:
            ticked = p.ticked_rows()
            if not ticked:
                self._status("Tick the positions to shift first (tick boxes in the first column of the table).")
                return
            rows = sorted(set(ticked) | {row})
        elif group_only:
            rows = [r for r in range(p.location_list.shape[0]) if str(p.location_ids[r]).split('-')[0] == group]
        else:
            rows = list(range(p.location_list.shape[0]))

        what = f"{len(rows)} position(s)" + (f" of group '{group}'" if group_only else (" (ticked + the reference)" if ticked_only else ""))
        text = (f"Selected position {row} is moved to the current coordinates and {what} are shifted by\n"
                f"dx = {delta[0]:+.4f} mm, dy = {delta[1]:+.4f} mm, dz = {delta[2] * 1000:+.1f} um.\n\nContinue?")
        if QMessageBox.question(self, 'Offset positions', text,
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return

        with p.undo_group():
            for r in rows:
                if r == row:
                    new = [current[0], current[1], current[2] if use_z else saved[2]]
                else:
                    old = p.location_list[r]
                    new = [old[0] + delta[0], old[1] + delta[1], old[2] + delta[2]]
                p.set_location(r, *new)
        self._status(f"Shifted {len(rows)} position(s) by dx={delta[0]:+.4f} mm, dy={delta[1]:+.4f} mm, "
                     f"dz={delta[2] * 1000:+.1f} um.")

    def _status(self, text):
        self.label_status.setText(text)
