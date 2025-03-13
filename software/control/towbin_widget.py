from qtpy.QtWidgets import (QWidget, QPushButton, QVBoxLayout, QHBoxLayout, QSpinBox,
                            QLineEdit, QCheckBox, QGridLayout, QMessageBox, QGroupBox,
                            QLabel, QTableWidgetItem)
from qtpy.QtWidgets import QFileDialog

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

        self.btn_update_position = QPushButton("Shift current position")
        grid_shift_points.addWidget(self.btn_update_position)
        self.btn_update_position.clicked.connect(self.update_position)

        self.btn_shift_all_positions = QPushButton("Shift all positions")
        grid_shift_points.addWidget(self.btn_shift_all_positions)
        self.btn_shift_all_positions.clicked.connect(self.shift_all_positions)

        '''
        # not really needed as users should only move relevant dimensions
        self.check_update_z = QCheckBox("Shift Z")
        self.check_update_z.setChecked(True)
        self.check_update_x = QCheckBox("Shift X")
        self.check_update_x.setChecked(True)
        self.check_update_y = QCheckBox("Shift Y")
        self.check_update_y.setChecked(True)
        self.hbox_check_update = QGroupBox()
        self.hbox_check_update.setLayout(QHBoxLayout())
        grid_shift_points.addWidget(self.hbox_check_update)
        self.hbox_check_update.layout().addWidget(self.check_update_z)
        self.hbox_check_update.layout().addWidget(self.check_update_x)
        self.hbox_check_update.layout().addWidget(self.check_update_y)'''

        self.btn_copy_z = QPushButton("Copy Z position to all")
        grid_shift_points.addWidget(self.btn_copy_z)
        self.btn_copy_z.clicked.connect(self.copy_z_to_all)
        self.btn_copy_z_to_same_id = QPushButton("Copy Z position to group ID")
        self.btn_copy_z_to_same_id.clicked.connect(self.copy_z_to_same_id)
        grid_shift_points.addWidget(self.btn_copy_z_to_same_id)

        """"
        # crop
        grid_crop = QGridLayout()
        self.layout.addLayout(grid_crop)

        self.spin_crop_x = QSpinBox()
        self.spin_crop_x.setMinimum(0)
        self.spin_crop_x.setMaximum(3000)
        self.spin_crop_x.setValue(1024)
        self.spin_crop_x.setSingleStep(10)
        self.spin_crop_x.setSuffix(' px')
        self.spin_crop_x.setFixedWidth(100)
        self.spin_crop_y = QSpinBox()
        self.spin_crop_y.setMinimum(0)
        self.spin_crop_y.setMaximum(3000)
        self.spin_crop_y.setValue(1024)
        self.spin_crop_y.setSingleStep(10)
        self.spin_crop_y.setSuffix(' px')
        self.spin_crop_y.setFixedWidth(100)

        grid_crop.addWidget(QLabel('Crop X'),1,1)
        grid_crop.addWidget(self.spin_crop_x,1,2)
        grid_crop.addWidget(QLabel('Crop Y'),1,3)
        grid_crop.addWidget(self.spin_crop_y,1,4)
        self.spin_crop_x.valueChanged.connect(self.update_crop)
        self.spin_crop_y.valueChanged.connect(self.update_crop)
        """

        grid_channels = QGridLayout()
        self.layout.addLayout(grid_channels)
        self.check_save_multichannel = QCheckBox("Save multichannel")
        self.check_save_multichannel.setChecked(True)
        grid_channels.addWidget(self.check_save_multichannel,2,1)

        self.btn_select_custom = QPushButton("Custom multipoint")
        self.btn_select_custom.setDefault(False)
        #self.btn_select_custom.setIcon(QIcon("icon/folder.png"))
        grid_crop.addWidget(self.btn_select_custom,3,1)
        self.btn_select_custom.clicked.connect(self.set_custom_file)

        self.lineEdit_customfile = QLineEdit()
        self.lineEdit_customfile.setReadOnly(True)
        self.lineEdit_customfile.setText("Choose a custom file")
        grid_crop.addWidget(self.lineEdit_customfile,3,2)

        self.parent.destroyed.connect(self.close_widget)


    def set_custom_file(self):
        dialog = QFileDialog()
        self.custom_file_path, _ = dialog.getOpenFileName(None, "Select File", ".", "Python Files (*.py)")
        self.lineEdit_customfile.setText(self.custom_file_path)

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

        x = self.parent.stage.get_pos().x_mm
        y = self.parent.stage.get_pos().y_mm
        z = self.parent.stage.get_pos().z_mm
        
        location_str = 'x: ' + str(round(x,3)) + ' mm, y: ' + str(round(y,3)) + ' mm, z: ' + str(round(1000*z,1)) + ' um'
        self.parent.dropdown_location_list.setItemText(index, location_str)
        
        
        self.parent.location_list[index] = [x, y, z]
        self.parent.table_location_list.setItem(index, 0, QTableWidgetItem(str(round(x,3))))
        self.parent.table_location_list.setItem(index, 1, QTableWidgetItem(str(round(y,3))))
        self.parent.table_location_list.setItem(index, 2, QTableWidgetItem(str(round(1000*z,1))))
        self.parent.navigationViewer.register_fov_to_image(x,y)

    def shift_all_positions(self):

        index = self.parent.dropdown_location_list.currentIndex()
        x_old = self.parent.location_list[index][0]
        y_old = self.parent.location_list[index][1]
        z_old = self.parent.location_list[index][2]

        x = self.parent.stage.get_pos().x_mm
        y = self.parent.stage.get_pos().y_mm
        z = self.parent.stage.get_pos().z_mm

        x_shift = x - x_old
        y_shift = y - y_old
        z_shift = z - z_old

        for i in range(self.parent.table_location_list.rowCount()):

            x_old = self.parent.location_list[i][0]
            y_old = self.parent.location_list[i][1]
            self.parent.navigationViewer.deregister_fov_to_image(x_old, y_old)

            x = float(self.parent.table_location_list.item(i, 0).text())
            y = float(self.parent.table_location_list.item(i, 1).text())
            z = float(self.parent.table_location_list.item(i, 2).text()) / 1000

            x += x_shift
            y += y_shift
            z += z_shift

            self.parent.table_location_list.setItem(i, 0, QTableWidgetItem(str(round(x,3))))
            self.parent.table_location_list.setItem(i, 1, QTableWidgetItem(str(round(y,3))))
            self.parent.table_location_list.setItem(i, 2, QTableWidgetItem(str(round(1000*z,1))))

            location_str = 'x: ' + str(round(x,3)) + ' mm, y: ' + str(round(y,3)) + ' mm, z: ' + str(round(1000*z,1)) + ' um'
            self.parent.dropdown_location_list.setItemText(index, location_str)
        
        
            self.parent.location_list[i] = [x, y, z]
            self.parent.table_location_list.setItem(i, 0, QTableWidgetItem(str(round(x,3))))
            self.parent.table_location_list.setItem(i, 1, QTableWidgetItem(str(round(y,3))))
            self.parent.table_location_list.setItem(i, 2, QTableWidgetItem(str(round(1000*z,1))))
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
