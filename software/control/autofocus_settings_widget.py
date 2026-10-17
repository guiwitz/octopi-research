"""Contrast AF tab of the main window.

A view on the autofocus controller: the controller (core.AutoFocusController) owns delta Z, N,
focus measure and autofocus channel; this widget only displays and edits them and can save
them to / load them from a user-chosen file. The last used file is applied again at startup.
"""
import os

from qtpy.QtWidgets import (QFrame, QGridLayout, QLabel, QPushButton, QSpinBox, QDoubleSpinBox,
                            QComboBox, QFileDialog, QMessageBox)

import control.autofocus_settings as autofocus_settings
from control._def import SCREW_PITCH_Z_MM, FULLSTEPS_PER_REV_Z

REGISTRY = autofocus_settings.REGISTRY
DEFAULT_DELTA_Z_UM = 1.524
DEFAULT_N_PLANES = 10


class AutofocusSettingsWidget(QFrame):

    def __init__(self, autofocusController, configurationManager, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.autofocusController = autofocusController
        self.configurationManager = configurationManager
        self.setFrameStyle(QFrame.Panel | QFrame.Raised)
        self._build_ui()

        # initial values (same defaults as before; not stored anywhere else)
        self.autofocusController.set_deltaZ(DEFAULT_DELTA_Z_UM)
        self.autofocusController.set_N(DEFAULT_N_PLANES)
        self._prepare_initial_focus_measure()
        self.refresh()

        # the widget is only a view: follow every change of the controller
        self.autofocusController.settings_changed.connect(self.refresh)
        self.autofocusController.autofocusFinished.connect(self._autofocus_is_finished)

        last_file = autofocus_settings.last_settings_file()
        if last_file is not None:
            self.load_settings_file(last_file, interactive=False)

    # ---- UI --------------------------------------------------------------------------
    def _build_ui(self):
        self.entry_delta = QDoubleSpinBox()
        self.entry_delta.setRange(0, 20)
        self.entry_delta.setSingleStep(0.2)
        self.entry_delta.setDecimals(3)
        self.entry_delta.setKeyboardTracking(False)

        self.entry_N = QSpinBox()
        self.entry_N.setRange(3, 20)
        self.entry_N.setSingleStep(1)
        self.entry_N.setKeyboardTracking(False)

        self.btn_autofocus = QPushButton('Autofocus')
        self.btn_autofocus.setDefault(False)
        self.btn_autofocus.setCheckable(True)
        self.btn_autofocus.setChecked(False)

        self.dropdown_measure = QComboBox()
        self.btn_load_custom = QPushButton('Load custom .py...')

        self.dropdown_channel = QComboBox()

        self.btn_save = QPushButton('Save settings...')
        self.btn_load = QPushButton('Load settings...')
        self.label_status = QLabel('')
        self.label_status.setStyleSheet('font-size: 8pt; color: gray;')
        self.label_status.setWordWrap(True)

        grid = QGridLayout()
        grid.addWidget(QLabel('delta Z (um)'), 0, 0)
        grid.addWidget(self.entry_delta, 0, 1)
        grid.addWidget(QLabel('N Z planes'), 0, 2)
        grid.addWidget(self.entry_N, 0, 3)
        grid.addWidget(self.btn_autofocus, 0, 4)
        grid.addWidget(QLabel('Focus measure'), 1, 0)
        grid.addWidget(self.dropdown_measure, 1, 1, 1, 3)
        grid.addWidget(self.btn_load_custom, 1, 4)
        grid.addWidget(QLabel('Autofocus channel\n(multipoint)'), 2, 0)
        grid.addWidget(self.dropdown_channel, 2, 1, 1, 3)
        grid.addWidget(self.btn_save, 3, 0, 1, 2)
        grid.addWidget(self.btn_load, 3, 2, 1, 3)
        grid.addWidget(self.label_status, 4, 0, 1, 5)
        grid.setRowStretch(grid.rowCount(), 1)
        self.setLayout(grid)

        self.btn_autofocus.clicked.connect(lambda: self.autofocusController.autofocus(False))
        self.entry_delta.valueChanged.connect(self._on_delta_changed)
        self.entry_N.valueChanged.connect(self.autofocusController.set_N)
        self.dropdown_measure.currentIndexChanged.connect(self._on_measure_selected)
        self.dropdown_channel.currentIndexChanged.connect(self._on_channel_selected)
        self.btn_load_custom.clicked.connect(self.load_custom_file_dialog)
        self.btn_save.clicked.connect(self.save_settings_dialog)
        self.btn_load.clicked.connect(self.load_settings_dialog)

    # ---- controller -> widget ----------------------------------------------------------
    def _channel_names(self):
        try:
            return [c.name for c in self.configurationManager.configurations]
        except AttributeError:
            return []

    def refresh(self):
        """Show the controller's current settings (no signals are sent back)."""
        c = self.autofocusController
        widgets_to_block = (self.entry_delta, self.entry_N, self.dropdown_measure, self.dropdown_channel)
        for w in widgets_to_block:
            w.blockSignals(True)
        try:
            if c.deltaZ is not None:
                self.entry_delta.setValue(c.deltaZ * 1000)
            if c.N is not None:
                self.entry_N.setValue(c.N)

            self.dropdown_measure.clear()
            keys = REGISTRY.keys()
            if c.focus_measure not in keys:
                keys.append(c.focus_measure)  # e.g. an unknown name from the configuration file
            for key in keys:
                self.dropdown_measure.addItem(REGISTRY.label(key), key)
            self.dropdown_measure.setCurrentIndex(keys.index(c.focus_measure))

            self.dropdown_channel.clear()
            names = self._channel_names()
            if c.af_channel not in names:
                names.append(c.af_channel)
            self.dropdown_channel.addItems(names)
            self.dropdown_channel.setCurrentText(c.af_channel)
        finally:
            for w in widgets_to_block:
                w.blockSignals(False)

    def _autofocus_is_finished(self):
        self.btn_autofocus.setChecked(False)

    # ---- widget -> controller ----------------------------------------------------------
    def _on_delta_changed(self, value):
        mm_per_ustep = SCREW_PITCH_Z_MM / (self.autofocusController.navigationController.z_microstepping * FULLSTEPS_PER_REV_Z)
        rounded = round(value / 1000 / mm_per_ustep) * mm_per_ustep * 1000
        self.autofocusController.set_deltaZ(rounded)  # emits settings_changed -> refresh() shows the rounded value

    def _on_measure_selected(self, index):
        key = self.dropdown_measure.itemData(index)
        if key is not None:
            self.autofocusController.set_focus_measure(key)

    def _on_channel_selected(self, index):
        name = self.dropdown_channel.itemText(index)
        if name:
            self.autofocusController.set_af_channel(name)

    def _prepare_initial_focus_measure(self):
        """A custom measure given in the configuration file must be loadable, otherwise use LAPE."""
        key = self.autofocusController.focus_measure
        if REGISTRY.is_custom(key):
            try:
                REGISTRY.get(key)
            except Exception as e:
                print(f'Custom focus measure {key} could not be loaded ({e}); using LAPE')
                self.autofocusController.focus_measure = 'LAPE'

    # ---- custom focus measure files ------------------------------------------------------
    def load_custom_file_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Load custom focus measure file', self._start_dir(),
                                              'Python files (*.py)')
        if path:
            self.load_custom_file(path)

    def load_custom_file(self, path):
        try:
            keys, problems = REGISTRY.load_file(path)
        except ValueError as e:
            QMessageBox.warning(self, 'Custom focus measure', str(e))
            return
        if problems:
            QMessageBox.warning(self, 'Custom focus measure',
                                'These functions were skipped:\n' + '\n'.join(problems))
        self.autofocusController.set_focus_measure(keys[0])  # emits settings_changed -> refresh()
        self._status(f'Loaded {len(keys)} focus measure(s) from {os.path.basename(path)}')

    # ---- settings files -------------------------------------------------------------------
    def _start_dir(self):
        last = autofocus_settings.last_settings_file()
        return os.path.dirname(last) if last else os.getcwd()

    def save_settings_dialog(self):
        path, _ = QFileDialog.getSaveFileName(self, 'Save autofocus settings',
                                              os.path.join(self._start_dir(), 'autofocus_settings.json'),
                                              'Autofocus settings (*.json)')
        if not path:
            return
        try:
            autofocus_settings.save_settings(path, self.autofocusController.get_settings())
        except OSError as e:
            QMessageBox.warning(self, 'Save autofocus settings', f'Could not save: {e}')
            return
        autofocus_settings.remember_settings_file(path)
        self._status(f'Saved to {os.path.basename(path)}')

    def load_settings_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Load autofocus settings', self._start_dir(),
                                              'Autofocus settings (*.json)')
        if path:
            self.load_settings_file(path, interactive=True)

    def load_settings_file(self, path, interactive=True):
        """Apply a settings file. Returns True if applied. Problems are shown (interactive) or printed."""
        def report(message):
            if interactive:
                QMessageBox.warning(self, 'Load autofocus settings', message)
            else:
                print('Autofocus settings:', message)

        try:
            settings = autofocus_settings.load_settings(path)
        except ValueError as e:
            report(str(e))
            return False

        notes = []
        c = self.autofocusController
        if 'focus_measure' in settings:
            key = settings['focus_measure']
            try:
                if REGISTRY.is_custom(key):
                    REGISTRY.get(key)
                c.focus_measure = key
            except Exception as e:
                notes.append(f'focus measure not applied ({e})')
        if 'af_channel' in settings:
            if settings['af_channel'] in self._channel_names():
                c.af_channel = settings['af_channel']
            else:
                notes.append(f"channel '{settings['af_channel']}' does not exist, kept '{c.af_channel}'")
        if 'delta_z_um' in settings:
            c.set_deltaZ(settings['delta_z_um'])
        if 'n_planes' in settings:
            c.set_N(settings['n_planes'])
        c.settings_changed.emit()

        autofocus_settings.remember_settings_file(path)
        self._status(f'Loaded {os.path.basename(path)}' + (' - ' + '; '.join(notes) if notes else ''))
        if notes:
            report('Loaded with exceptions: ' + '; '.join(notes))
        return True

    def _status(self, text):
        self.label_status.setText(text)
