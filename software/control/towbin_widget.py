from qtpy.QtWidgets import (QWidget, QPushButton, QVBoxLayout,QSpinBox,
                            QLineEdit, QCheckBox, QGridLayout, QMessageBox, QLabel, QTableWidgetItem,
                            QComboBox)

from qtpy.QtCore import Qt

class TowbinWidget(QWidget):
    def __init__(self, parent=None, camera=None, multipoint_controller=None,
                 autofocus_controller=None, stream_handler=None):
        super(TowbinWidget, self).__init__(parent)

        self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)
        self.setWindowTitle('Towbin Widget')
        self.move(100, 100)

        self.parent = parent
        # Direct references passed in from creator
        self.camera = camera
        self.multipoint_controller = multipoint_controller
        self.autofocus_controller = autofocus_controller
        self.stream_handler = stream_handler

        self.show()

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

        # camera ROI (the only crop: hardware ROI, always centred on the sensor, multiples of 8)
        # The software crops of the stream, multipoint and autofocus are kept equal to the ROI,
        # i.e. there is no additional software cropping.
        grid_crop = QGridLayout()
        self.layout.addLayout(grid_crop)

        sensor_w, sensor_h = self._sensor_size()
        self.spin_crop_x = QSpinBox()
        self.spin_crop_x.setMinimum(16)
        self.spin_crop_x.setMaximum(sensor_w)
        self.spin_crop_x.setSingleStep(8)
        self.spin_crop_x.setSuffix(' px')
        self.spin_crop_x.setFixedWidth(100)
        # Only emit valueChanged on Enter or focus-out (not on every digit)
        self.spin_crop_x.setKeyboardTracking(False)
        self.spin_crop_y = QSpinBox()
        self.spin_crop_y.setMinimum(16)
        self.spin_crop_y.setMaximum(sensor_h)
        self.spin_crop_y.setSingleStep(8)
        self.spin_crop_y.setSuffix(' px')
        self.spin_crop_y.setFixedWidth(100)
        self.spin_crop_y.setKeyboardTracking(False)

        # Initialize spinbox values from current camera ROI and bring the software crops in line with it
        current_width, current_height = self._current_roi_size()
        self.spin_crop_x.setValue(current_width)
        self.spin_crop_y.setValue(current_height)
        self._apply_software_crops(current_width, current_height)

        grid_crop.addWidget(QLabel('ROI width'),1,1)
        grid_crop.addWidget(self.spin_crop_x,1,2)
        grid_crop.addWidget(QLabel('ROI height'),1,3)
        grid_crop.addWidget(self.spin_crop_y,1,4)
        self.spin_crop_x.valueChanged.connect(self.update_crop)
        self.spin_crop_y.valueChanged.connect(self.update_crop)

        # camera mode / bit depth
        self.dropdown_pixel_format = QComboBox()
        formats = ['MONO8', 'MONO12', 'MONO14', 'MONO16']
        if getattr(self.camera, 'is_color', None):
            formats += ['BAYER_RG8', 'BAYER_RG12']
        self.dropdown_pixel_format.addItems(formats)
        current_format = getattr(self.camera, 'pixel_format', None)
        if current_format in formats:
            self.dropdown_pixel_format.setCurrentText(current_format)
        self.dropdown_pixel_format.currentTextChanged.connect(self.set_pixel_format)
        grid_crop.addWidget(QLabel('Camera mode'),2,1)
        grid_crop.addWidget(self.dropdown_pixel_format,2,2)

        grid_channels = QGridLayout()
        self.layout.addLayout(grid_channels)
        self.check_save_multichannel = QCheckBox("Save multichannel")
        self.check_save_multichannel.setChecked(True)
        grid_channels.addWidget(self.check_save_multichannel,0,1)

        self.parent.destroyed.connect(self.close_widget)

    def close_widget(self):
        self.close()

    def showEvent(self, event):
        """After showing the window, sync crop spinboxes with current camera ROI."""
        # After showing the window, remove the stay on top hint
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowStaysOnTopHint)
        self.show()

        # Sync spinboxes with current camera ROI without triggering update_crop()
        # Guard: __init__ calls show() which triggers showEvent - camera may not be set yet
        if not hasattr(self, 'camera') or self.camera is None or not hasattr(self, 'spin_crop_y'):
            return
        current_width, current_height = self._current_roi_size()
        self.spin_crop_x.blockSignals(True)
        self.spin_crop_y.blockSignals(True)
        self.spin_crop_x.setValue(current_width)
        self.spin_crop_y.setValue(current_height)
        self.spin_crop_x.blockSignals(False)
        self.spin_crop_y.blockSignals(False)
    
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

    # ---- camera ROI / crop -------------------------------------------------------------

    def _sensor_size(self):
        """Full sensor size in px (falls back to 3000 x 3000 if the camera does not report it)."""
        try:
            return int(self.camera.WidthMax), int(self.camera.HeightMax)
        except (AttributeError, TypeError):
            return 3000, 3000

    def _current_roi_size(self):
        """Current hardware ROI size in px."""
        sensor_w, sensor_h = self._sensor_size()
        try:
            width = self.camera.Width if self.camera.Width else sensor_w
            height = self.camera.Height if self.camera.Height else sensor_h
        except AttributeError:
            width, height = sensor_w, sensor_h
        return int(width), int(height)

    @staticmethod
    def _valid_size(value, sensor_size):
        """Nearest multiple of 8, at least 16 and at most the sensor size (rounded down to a multiple of 8)."""
        largest = max(16, (int(sensor_size) // 8) * 8)
        return int(min(max(int(value / 8 + 0.5) * 8, 16), largest))

    def _apply_software_crops(self, width, height):
        """Keep every software crop identical to the hardware ROI (= no software cropping)."""
        if self.multipoint_controller is not None:
            self.multipoint_controller.crop_width = width
            self.multipoint_controller.crop_height = height
        if self.stream_handler is not None:
            self.stream_handler.set_crop(width, height)
        if self.autofocus_controller is not None:
            self.autofocus_controller.set_crop(width, height)

    def update_crop(self):
        """Set the camera's hardware ROI, centred on the sensor, with width/height rounded to multiples of 8.

        The stream, multipoint and autofocus software crops are set to the same size, so the ROI
        is the only crop applied to live view, autofocus and saved images.
        """
        if self.camera is None:
            print("Error: camera not set on TowbinWidget")
            return

        sensor_width, sensor_height = self._sensor_size()
        crop_width = self._valid_size(self.spin_crop_x.value(), sensor_width)
        crop_height = self._valid_size(self.spin_crop_y.value(), sensor_height)

        # show the rounded values without re-triggering this method
        for spin, value in ((self.spin_crop_x, crop_width), (self.spin_crop_y, crop_height)):
            if spin.value() != value:
                spin.blockSignals(True)
                spin.setValue(value)
                spin.blockSignals(False)

        # centred offsets, multiples of 8
        offset_x = max(0, ((sensor_width - crop_width) // 2) // 8 * 8)
        offset_y = max(0, ((sensor_height - crop_height) // 2) // 8 * 8)

        self.camera.set_ROI(
            offset_x=offset_x,
            offset_y=offset_y,
            width=crop_width,
            height=crop_height
        )
        print(f"Camera ROI set: offset_x={offset_x}, offset_y={offset_y}, "
              f"width={crop_width}, height={crop_height}")

        self._apply_software_crops(crop_width, crop_height)

    def set_pixel_format(self, pixel_format):
        """Camera mode / bit depth."""
        if self.camera is not None:
            self.camera.set_pixel_format(pixel_format)
