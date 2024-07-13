from qtpy.QtWidgets import (QWidget, QPushButton, QVBoxLayout,QSpinBox,
                            QLineEdit, QCheckBox, QGridLayout, QMessageBox, QLabel, QTableWidgetItem)

from qtpy.QtCore import Qt

class TowbinWidget(QWidget):
    def __init__(self, parent=None):
        super(TowbinWidget, self).__init__(parent)

        self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)
        self.setWindowTitle('Towbin Widget')
        self.move(100, 100)
        self.show()

        self.parent = parent

        self.layout = QVBoxLayout()
        self.setLayout(self.layout)

        grid_edit_points_id = QGridLayout()
        self.layout.addLayout(grid_edit_points_id)

        self.editable_text = QLineEdit()
        grid_edit_points_id.addWidget(self.editable_text)

        self.edit_id_btn = QPushButton("Edit ID(s)")
        self.edit_id_btn.clicked.connect(self.edit_id_selected_location)
        grid_edit_points_id.addWidget(self.edit_id_btn)

        self.check_id_sequential = QCheckBox("ID Sequential")
        grid_edit_points_id.addWidget(self.check_id_sequential)

        grid_shift_points = QGridLayout()
        self.layout.addLayout(grid_shift_points)

        self.btn_update_position = QPushButton("Update Position")
        grid_shift_points.addWidget(self.btn_update_position)
        self.btn_update_position.clicked.connect(self.update_position)

        self.btn_copy_z = QPushButton("Copy Z to all")
        grid_shift_points.addWidget(self.btn_copy_z)
        self.btn_copy_z.clicked.connect(self.copy_z_to_all)
        self.btn_copy_z_to_same_id = QPushButton("Copy Z to group ID")
        self.btn_copy_z_to_same_id.clicked.connect(self.copy_z_to_same_id)
        grid_shift_points.addWidget(self.btn_copy_z_to_same_id)

        # crop
        grid_crop = QGridLayout()
        self.layout.addLayout(grid_crop)

        self.spin_crop_x = QSpinBox()
        self.spin_crop_x.setMinimum(0)
        self.spin_crop_x.setMaximum(3000)
        self.spin_crop_x.setValue(1000)
        self.spin_crop_x.setSingleStep(10)
        self.spin_crop_x.setSuffix(' px')
        self.spin_crop_x.setFixedWidth(100)
        self.spin_crop_y = QSpinBox()
        self.spin_crop_y.setMinimum(0)
        self.spin_crop_y.setMaximum(3000)
        self.spin_crop_y.setValue(1000)
        self.spin_crop_y.setSingleStep(10)
        self.spin_crop_y.setSuffix(' px')
        self.spin_crop_y.setFixedWidth(100)

        grid_crop.addWidget(QLabel('Crop X'),1,1)
        grid_crop.addWidget(self.spin_crop_x,1,2)
        grid_crop.addWidget(QLabel('Crop Y'),1,3)
        grid_crop.addWidget(self.spin_crop_y,1,4)
        self.spin_crop_x.valueChanged.connect(self.update_crop)
        self.spin_crop_y.valueChanged.connect(self.update_crop)

        self.parent.destroyed.connect(self.close_widget)

    def close_widget(self):
        self.close()

    def showEvent(self, event):
        # After showing the window, remove the stay on top hint
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowStaysOnTopHint)
        self.show()
    
    def edit_id_selected_location(self):
        """Edit the ID of the selected locations in the table. If the checkbox
        is checked, the ID will be sequential."""
        
        if '-' in self.editable_text.text():
            msg = QMessageBox()
            msg.setText("Please avoid the character '-' in the ID")
            msg.exec_()

        selected_items = self.parent.table_location_list.selectedItems()
        rows_to_edit = set([s.row() for s in selected_items])
        for ind, row in enumerate(rows_to_edit):
            item = self.parent.table_location_list.item(row, 3)
            if self.check_id_sequential.isChecked():
                item.setText(f'{self.editable_text.text()}-{ind}')
            else:
                item.setText(self.editable_text.text())

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

    def copy_z_to_all(self):
        """Copy the Z value of the selected location to all the locations in the
        table."""
        
        selected_items = self.parent.table_location_list.selectedItems()
        row = selected_items[0].row()
        z_value = self.parent.table_location_list.item(row, 2).text()
        for row in range(self.parent.table_location_list.rowCount()):
            item = self.parent.table_location_list.item(row, 2)
            item.setText(z_value)

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

    def update_crop(self):
        """Update the crop values in the multipoint controller and the stream
        handler."""

        self.parent.multipointController.crop_height = self.spin_crop_y.value()
        self.parent.multipointController.crop_width = self.spin_crop_x.value()

        self.parent.multipointController.parent.streamHandler.set_crop(
            self.spin_crop_x.value(), self.spin_crop_y.value()
        )
