from qtpy.QtWidgets import (QWidget, QPushButton, QVBoxLayout,QSpinBox,
                            QLineEdit, QCheckBox, QGridLayout, QMessageBox, QLabel, QTableWidgetItem)

from qtpy.QtCore import Qt

class TowbinWidget(QWidget):
    def __init__(self, parent=None, camera=None, multipoint_controller=None,
                 autofocus_controller=None, stream_handler=None,
                 camera_setting_widget=None):
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
        self.camera_setting_widget = camera_setting_widget

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
        # Only emit valueChanged on Enter or focus-out (not on every digit)
        self.spin_crop_x.setKeyboardTracking(False)
        self.spin_crop_y = QSpinBox()
        self.spin_crop_y.setMinimum(0)
        self.spin_crop_y.setMaximum(3000)
        self.spin_crop_y.setValue(1000)
        self.spin_crop_y.setSingleStep(10)
        self.spin_crop_y.setSuffix(' px')
        self.spin_crop_y.setFixedWidth(100)
        self.spin_crop_y.setKeyboardTracking(False)

        # Initialize spinbox values from current camera ROI (if camera provided)
        if self.camera is not None:
            try:
                current_width = self.camera.Width if self.camera.Width else self.camera.WidthMax
                current_height = self.camera.Height if self.camera.Height else self.camera.HeightMax
                self.spin_crop_x.setMaximum(max(3000, current_width))
                self.spin_crop_y.setMaximum(max(3000, current_height))
                self.spin_crop_x.setValue(current_width)
                self.spin_crop_y.setValue(current_height)
            except AttributeError:
                pass

        grid_crop.addWidget(QLabel('Crop X'),1,1)
        grid_crop.addWidget(self.spin_crop_x,1,2)
        grid_crop.addWidget(QLabel('Crop Y'),1,3)
        grid_crop.addWidget(self.spin_crop_y,1,4)
        self.spin_crop_x.valueChanged.connect(self.update_crop)
        self.spin_crop_y.valueChanged.connect(self.update_crop)

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
        if not hasattr(self, 'camera') or self.camera is None:
            return
        try:
            current_width = self.camera.Width if self.camera.Width else self.camera.WidthMax
            current_height = self.camera.Height if self.camera.Height else self.camera.HeightMax
            self.spin_crop_x.blockSignals(True)
            self.spin_crop_y.blockSignals(True)
            self.spin_crop_x.setValue(current_width)
            self.spin_crop_y.setValue(current_height)
            self.spin_crop_x.blockSignals(False)
            self.spin_crop_y.blockSignals(False)
        except AttributeError:
            pass
    
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
        """Update the crop values by setting the camera hardware ROI and software crop.
        
        This method:
        1. Sets the camera's hardware ROI (centered on sensor)
        2. Configures software crop to match (no additional cropping)
        3. Updates multipoint controller with the new crop dimensions
        4. Updates autofocus controller with the same crop dimensions
        5. Updates camera settings widget spinboxes to reflect new ROI
        """
        
        if self.camera is None:
            print("Error: camera not set on TowbinWidget")
            return
        
        crop_width = self.spin_crop_x.value()
        crop_height = self.spin_crop_y.value()
        
        # Get camera sensor dimensions
        sensor_width = self.camera.WidthMax
        sensor_height = self.camera.HeightMax
        
        # Calculate centered ROI offsets
        offset_x = int((sensor_width - crop_width) / 2)
        offset_y = int((sensor_height - crop_height) / 2)
        
        # Ensure offsets are non-negative and within bounds
        offset_x = max(0, min(offset_x, sensor_width - crop_width))
        offset_y = max(0, min(offset_y, sensor_height - crop_height))
        
        # 1. Set camera hardware ROI (centered)
        self.camera.set_ROI(
            offset_x=offset_x,
            offset_y=offset_y,
            width=crop_width,
            height=crop_height
        )
        print(f"Camera ROI set: offset_x={offset_x}, offset_y={offset_y}, "
              f"width={crop_width}, height={crop_height}")
        
        # 2. Set software crop on multipoint controller
        if self.multipoint_controller is not None:
            self.multipoint_controller.crop_width = crop_width
            self.multipoint_controller.crop_height = crop_height
        
        # 3. Set software crop on stream handler
        if self.stream_handler is not None:
            self.stream_handler.set_crop(crop_width, crop_height)
        
        # 4. Update autofocus controller with same crop dimensions
        if self.autofocus_controller is not None:
            self.autofocus_controller.set_crop(crop_width, crop_height)
        
        # 5. Update camera settings widget spinboxes to reflect new ROI
        self._update_camera_settings_widget(crop_width, crop_height, offset_x, offset_y)
    
    def _update_camera_settings_widget(self, width, height, offset_x, offset_y):
        """Update the CameraSettingWidget spinboxes with current ROI values."""
        if self.camera_setting_widget is None:
            return
        
        w = self.camera_setting_widget
        
        # Block signals to prevent triggering set_ROI() recursively
        w.entry_ROI_width.blockSignals(True)
        w.entry_ROI_height.blockSignals(True)
        w.entry_ROI_offset_x.blockSignals(True)
        w.entry_ROI_offset_y.blockSignals(True)
        
        # Update spinbox values
        w.entry_ROI_width.setValue(width)
        w.entry_ROI_height.setValue(height)
        w.entry_ROI_offset_x.setValue(offset_x)
        w.entry_ROI_offset_y.setValue(offset_y)
        
        # Unblock signals
        w.entry_ROI_width.blockSignals(False)
        w.entry_ROI_height.blockSignals(False)
        w.entry_ROI_offset_x.blockSignals(False)
        w.entry_ROI_offset_y.blockSignals(False)
