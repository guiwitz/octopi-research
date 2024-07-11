from qtpy.QtWidgets import (QWidget, QPushButton, QVBoxLayout,QSpinBox,
                            QLineEdit, QCheckBox, QGridLayout, QMessageBox, QLabel)

class TowbinWidget(QWidget):
    def __init__(self, parent=None):
        super(TowbinWidget, self).__init__(parent)

        self.parent = parent

        self.layout = QVBoxLayout()
        self.setLayout(self.layout)

        grid_edit_points_id = QGridLayout()
        self.layout.addLayout(grid_edit_points_id)

        self.editable_text = QLineEdit()
        grid_edit_points_id.addWidget(self.editable_text)

        self.edit_id_btn = QPushButton("Edit")
        self.edit_id_btn.clicked.connect(self.edit_id_selected_location)
        grid_edit_points_id.addWidget(self.edit_id_btn)

        self.check_id_sequential = QCheckBox("Check ID Sequential")
        grid_edit_points_id.addWidget(self.check_id_sequential)

        grid_shift_points = QGridLayout()
        self.layout.addLayout(grid_shift_points)

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
