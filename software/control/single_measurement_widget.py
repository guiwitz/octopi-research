"""Single measurement tab: a quick image in one or several channels, or a fast (video-rate) timelapse, at the CURRENT stage position.

Both are small jobs (control/jobs): the tab writes the job file (cache/single_measurement_job.py; it is also copied into the experiment folder as
job.py, so what was done is documented and can be reused in the JOBs tab) and runs it through the same MultiPointController/worker as the JOBs tab.
The stage does not move. Files and settings are those of the JOBs: Time0000_Point0000.tiff (all selected channels in one multipage file) or
single files per channel, Time0000_Point0000_FastMovie.tiff for the fast timelapse (see control/jobs/README.md).
"""
import os
import json
import traceback

from qtpy.QtCore import Signal
from qtpy.QtWidgets import (QFrame, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QLineEdit, QPushButton, QFileDialog, QMessageBox, QPlainTextEdit,
                            QDoubleSpinBox, QSpinBox, QComboBox, QCheckBox, QGroupBox, QListWidget, QListWidgetItem)
from qtpy.QtCore import Qt

from control.jobs import make_points, dry_run, describe
from control.jobs.api import MAX_BUFFER_BYTES
from control.resizable_panel import make_combo_compact

CACHE_FILE = os.path.join('cache', 'single_measurement_settings.json')
JOB_FILE = os.path.join('cache', 'single_measurement_job.py')
UNSUPPORTED_CHANNELS = ('RGB', 'USB Spectrometer')

CAPTURE_JOB = '''"""Single image(s) at the current stage position (written by the Single measurement tab)."""


def run_job(scope):
    """WHAT IT DOES: one image in each of the selected channels at the current stage position (the stage does not move).

    YOU PROVIDE: nothing."""
    scope.capture_channels({channels})
'''

FAST_JOB = '''"""Fast timelapse at the current stage position (written by the Single measurement tab)."""


def run_job(scope):
    """WHAT IT DOES: a video-rate movie in one channel at the current stage position (the stage does not move): the camera runs freely at {fps:g}
    frames/s, the frames are kept in memory and written afterwards ({compression}); the camera settings are restored afterwards.

    YOU PROVIDE: a channel whose exposure is shorter than the frame period; an intensity that does not saturate (8-bit images saturate at 255)."""
    scope.fast_movie({channel!r}, fps={fps!r}, duration_s={duration!r}, roi={roi!r}, bits={bits!r}, exposure_ms={exposure!r}, gain={gain!r}, compression={compression!r})
'''


class SingleMeasurementWidget(QFrame):

    signal_acquisition_started = Signal(bool)     # same meaning as in the other acquisition tabs (locks the stage controls)

    def __init__(self, multipointController, configurationManager, positionsWidget=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mpc = multipointController
        self.configurationManager = configurationManager
        self.positionsWidget = positionsWidget
        self.running = False
        self._kind = None
        self._build()
        self._load_cache()
        self._update_info()
        self.mpc.signal_job_message.connect(self._on_job_message)
        if hasattr(self.configurationManager, 'configurations_changed'):
            self.configurationManager.configurations_changed.connect(self.refresh_channels)
        self.mpc.acquisitionFinished.connect(self.acquisition_is_finished)

    # --- layout
    def refresh_channels(self, *_):
        """The channel list changed (Illuminations tab): rebuild the checkable list and the fast-timelapse channel, keeping what was checked/selected"""
        checked = set(self.selected_channels())
        fast = self.combo_fast_channel.currentData()
        self.list_channels.blockSignals(True)
        self.list_channels.clear()
        for c in self._channels():
            it = QListWidgetItem(f'{c.name}   ({c.exposure_time:g} ms)')
            it.setData(Qt.UserRole, c.name)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if c.name in checked else Qt.Unchecked)
            self.list_channels.addItem(it)
        self.list_channels.blockSignals(False)
        self.combo_fast_channel.blockSignals(True)
        self.combo_fast_channel.clear()
        for c in self._channels():
            self.combo_fast_channel.addItem(c.name, c.name)
        i = self.combo_fast_channel.findData(fast)
        self.combo_fast_channel.setCurrentIndex(max(0, i))
        self.combo_fast_channel.blockSignals(False)
        self._update_info()

    def _channels(self):
        return [c for c in self.configurationManager.configurations if not any(u in c.name for u in UNSUPPORTED_CHANNELS)]

    def _build(self):
        self.lineEdit_savingDir = QLineEdit()
        self.btn_savingDir = QPushButton('Saving dir...')
        self.lineEdit_experimentID = QLineEdit('single')
        self.combo_save_as = QComboBox()
        for value, text in (('multipage', 'multipage TIFF, written directly'), ('merge', 'single files, merged into multipage TIFF later'), ('single', 'single files')):
            self.combo_save_as.addItem(text, value)
        self.combo_save_as.setToolTip('multipage: all channels of an image in ONE file (Time0000_Point0000.tiff); single files: one TIFF per channel in a folder (0/stage_0_0_0_<channel>.tiff); '
                                      'merge: single files first, merged into the multipage file afterwards (safer for very large images). As in the JOBs tab.')

        # single capture
        self.list_channels = QListWidget()
        self.list_channels.setMaximumHeight(150)
        for c in self._channels():
            it = QListWidgetItem(f'{c.name}   ({c.exposure_time:g} ms)')
            it.setData(Qt.UserRole, c.name)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Unchecked)
            self.list_channels.addItem(it)
        self.btn_all = QPushButton('All'); self.btn_none = QPushButton('None')
        self.btn_capture = QPushButton('Capture now')
        self.btn_capture.setStyleSheet('background-color: #C2C2FF')
        self.btn_capture.setToolTip('One image in each checked channel at the current stage position, saved with the settings above')

        # fast timelapse
        self.combo_fast_channel = QComboBox()
        for c in self._channels():
            self.combo_fast_channel.addItem(c.name, c.name)
        self.spin_fps = QDoubleSpinBox(); self.spin_fps.setRange(0.1, 500); self.spin_fps.setDecimals(2); self.spin_fps.setValue(25)
        self.spin_duration = QDoubleSpinBox(); self.spin_duration.setRange(0.1, 3600); self.spin_duration.setDecimals(1); self.spin_duration.setValue(5)
        self.spin_roi = QSpinBox(); self.spin_roi.setRange(0, 20000); self.spin_roi.setSingleStep(16); self.spin_roi.setValue(2000)
        self.spin_roi.setSpecialValueText('current')
        self.spin_roi.setToolTip('Square region in the centre of the sensor (pixels). 0 = keep the current camera region. Smaller regions allow higher frame rates.')
        self.combo_bits = QComboBox()
        for text, v in (('8 bit', 8), ('16 bit', 16), ('current', None)):
            self.combo_bits.addItem(text, v)
        self.combo_bits.setToolTip('8 bit reads out about twice as fast and needs half the memory, but saturates at 255: use less light/exposure. '
                                   'The camera is set back to its previous bit depth afterwards.')
        self.check_exposure = QCheckBox('exposure (ms)')
        self.spin_exposure = QDoubleSpinBox(); self.spin_exposure.setRange(0.01, 1000); self.spin_exposure.setDecimals(2); self.spin_exposure.setValue(10)
        self.check_gain = QCheckBox('analog gain')
        self.spin_gain = QDoubleSpinBox(); self.spin_gain.setRange(0, 48); self.spin_gain.setDecimals(1); self.spin_gain.setValue(0)
        self.combo_compression = QComboBox()
        for text, v in (('uncompressed', 'none'), ('lossless (zlib, slow)', 'lossless')):
            self.combo_compression.addItem(text, v)
        self.label_info = QLabel('')
        self.label_info.setWordWrap(True)
        self.btn_fast = QPushButton('Record fast timelapse')
        self.btn_fast.setStyleSheet('background-color: #C2C2FF')
        self.btn_fast.setToolTip('Video-rate movie in the chosen channel at the current stage position; the light is on only while recording')
        self.btn_abort = QPushButton('Abort')
        self.btn_abort.setEnabled(False)
        self.label_status = QLabel('')
        self.label_status.setWordWrap(True)
        self.log = QPlainTextEdit(); self.log.setReadOnly(True); self.log.setMaximumBlockCount(3000)

        self.lineEdit_savingDir.setMinimumWidth(250)
        self.lineEdit_savingDir.setToolTip('Saving directory (full path)')
        self.lineEdit_savingDir.textChanged.connect(lambda t, w=self.lineEdit_savingDir: w.setToolTip(t or 'Saving directory (full path)'))
        self.lineEdit_experimentID.setMinimumWidth(160)
        make_combo_compact(self.combo_save_as, 12, 420)   # long item texts must not widen the control panel
        row1 = QHBoxLayout(); row1.addWidget(QLabel('Saving dir')); row1.addWidget(self.lineEdit_savingDir, 1); row1.addWidget(self.btn_savingDir)
        row1b = QHBoxLayout(); row1b.addWidget(QLabel('Experiment ID')); row1b.addWidget(self.lineEdit_experimentID, 1)
        row1b.addSpacing(10); row1b.addWidget(QLabel('Save as')); row1b.addWidget(self.combo_save_as, 1)

        box1 = QGroupBox('Single time capture (current position)')
        v1 = QVBoxLayout(box1)
        v1.addWidget(QLabel('Channels (exposure of the channel settings in brackets):'))
        v1.addWidget(self.list_channels)
        r = QHBoxLayout(); r.addWidget(self.btn_all); r.addWidget(self.btn_none); r.addStretch(1); r.addWidget(self.btn_capture); v1.addLayout(r)

        box2 = QGroupBox('Fast timelapse (current position)')
        g = QGridLayout(box2)
        g.addWidget(QLabel('Channel'), 0, 0); g.addWidget(self.combo_fast_channel, 0, 1, 1, 3)
        g.addWidget(QLabel('Frames/s'), 1, 0); g.addWidget(self.spin_fps, 1, 1); g.addWidget(QLabel('Duration (s)'), 1, 2); g.addWidget(self.spin_duration, 1, 3)
        g.addWidget(QLabel('Region (px)'), 2, 0); g.addWidget(self.spin_roi, 2, 1); g.addWidget(QLabel('Bit depth'), 2, 2); g.addWidget(self.combo_bits, 2, 3)
        g.addWidget(self.check_exposure, 3, 0); g.addWidget(self.spin_exposure, 3, 1); g.addWidget(self.check_gain, 3, 2); g.addWidget(self.spin_gain, 3, 3)
        g.addWidget(QLabel('File'), 4, 0); g.addWidget(self.combo_compression, 4, 1, 1, 3)
        g.addWidget(self.label_info, 5, 0, 1, 4)
        g.addWidget(self.btn_fast, 6, 0, 1, 4)

        layout = QVBoxLayout()
        layout.addLayout(row1); layout.addLayout(row1b); layout.addWidget(box1); layout.addWidget(box2)
        r = QHBoxLayout(); r.addWidget(self.label_status, 1); r.addWidget(self.btn_abort); layout.addLayout(r)
        layout.addWidget(self.log, 1)
        self.setLayout(layout)

        self.btn_savingDir.clicked.connect(self.select_saving_dir)
        self.btn_all.clicked.connect(lambda: self._check_all(True))
        self.btn_none.clicked.connect(lambda: self._check_all(False))
        self.btn_capture.clicked.connect(self.start_capture)
        self.btn_fast.clicked.connect(self.start_fast)
        self.btn_abort.clicked.connect(self.abort)
        for w in (self.spin_fps, self.spin_duration, self.spin_roi, self.spin_exposure, self.spin_gain):
            w.valueChanged.connect(self._update_info)
        for w in (self.combo_bits, self.combo_fast_channel):
            w.currentIndexChanged.connect(self._update_info)
        self.check_exposure.toggled.connect(self._update_info)

    def showEvent(self, event):
        super().showEvent(event)
        pw = self.positionsWidget
        if pw is not None and not self.lineEdit_savingDir.text() and pw.lineEdit_savingDir.text():
            self.lineEdit_savingDir.setText(pw.lineEdit_savingDir.text())
        self._update_info()

    # --- persistence
    def _settings(self):
        return dict(saving_dir=self.lineEdit_savingDir.text(), experiment_id=self.lineEdit_experimentID.text(), save_as=self.combo_save_as.currentData(),
                    channels=self.selected_channels(), fast_channel=self.combo_fast_channel.currentData(), fps=self.spin_fps.value(),
                    duration=self.spin_duration.value(), roi=self.spin_roi.value(), bits=self.combo_bits.currentData(),
                    override_exposure=self.check_exposure.isChecked(), exposure=self.spin_exposure.value(), override_gain=self.check_gain.isChecked(),
                    gain=self.spin_gain.value(), compression=self.combo_compression.currentData())

    def _load_cache(self):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                d = json.load(f)
        except Exception:
            return
        try:
            self.lineEdit_savingDir.setText(d.get('saving_dir', ''))
            self.lineEdit_experimentID.setText(d.get('experiment_id', 'single'))
            for combo, key in ((self.combo_save_as, 'save_as'), (self.combo_fast_channel, 'fast_channel'), (self.combo_bits, 'bits'), (self.combo_compression, 'compression')):
                k = combo.findData(d.get(key))
                if k >= 0:
                    combo.setCurrentIndex(k)
            for w, key in ((self.spin_fps, 'fps'), (self.spin_duration, 'duration'), (self.spin_roi, 'roi'), (self.spin_exposure, 'exposure'), (self.spin_gain, 'gain')):
                if key in d:
                    w.setValue(d[key])
            self.check_exposure.setChecked(bool(d.get('override_exposure', False)))
            self.check_gain.setChecked(bool(d.get('override_gain', False)))
            chosen = set(d.get('channels', []))
            for i in range(self.list_channels.count()):
                it = self.list_channels.item(i)
                it.setCheckState(Qt.Checked if it.data(Qt.UserRole) in chosen else Qt.Unchecked)
        except Exception:
            pass

    def _save_cache(self):
        try:
            os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
            with open(CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump(self._settings(), f)
        except Exception:
            pass

    # --- helpers
    def append_log(self, text):
        self.log.appendPlainText(text)

    def _say(self, text):
        self.label_status.setText(text.splitlines()[0])
        self.log.appendPlainText(text)

    def _warn(self, title, text):
        QMessageBox.warning(self, title, text)

    def select_saving_dir(self):
        d = QFileDialog.getExistingDirectory(self, 'Select folder')
        if d:
            self.lineEdit_savingDir.setText(d)
            self._save_cache()

    def _check_all(self, on):
        for i in range(self.list_channels.count()):
            self.list_channels.item(i).setCheckState(Qt.Checked if on else Qt.Unchecked)

    def selected_channels(self):
        return [self.list_channels.item(i).data(Qt.UserRole) for i in range(self.list_channels.count())
                if self.list_channels.item(i).checkState() == Qt.Checked]

    def _channel_config(self, name):
        return next((c for c in self.configurationManager.configurations if c.name == name), None)

    def _channel_infos(self):
        return {c.name: dict(exposure_ms=c.exposure_time, gain=c.analog_gain, intensity=c.illumination_intensity) for c in self._channels()}

    def _fast_parameters(self):
        return dict(channel=self.combo_fast_channel.currentData(), fps=float(self.spin_fps.value()), duration=float(self.spin_duration.value()),
                    roi=int(self.spin_roi.value()) or None, bits=self.combo_bits.currentData(),
                    exposure=float(self.spin_exposure.value()) if self.check_exposure.isChecked() else None,
                    gain=float(self.spin_gain.value()) if self.check_gain.isChecked() else None, compression=self.combo_compression.currentData())

    def _frame_geometry(self):
        """(width, height, bytes per pixel) of the movie frames as far as they are known now"""
        p = self._fast_parameters()
        cam = getattr(self.mpc, 'camera', None)
        w = h = None
        if p['roi']:
            w = h = p['roi']
        else:
            w, h = getattr(cam, 'Width', None), getattr(cam, 'Height', None)
        bpp = 1 if p['bits'] == 8 else 2 if p['bits'] == 16 else int(getattr(cam, 'pixel_size_byte', 2) or 2)
        return w, h, bpp

    def _update_info(self, *_):
        try:
            p = self._fast_parameters()
            n = max(1, int(round(p['fps'] * p['duration'])))
            w, h, bpp = self._frame_geometry()
            cfg = self._channel_config(p['channel'])
            exp = p['exposure'] if p['exposure'] is not None else (cfg.exposure_time if cfg is not None else None)
            text = f'{n} frames'
            if w and h:
                text += f' of {w} x {h} px = {n * w * h * bpp / 1e9:.2f} GB (limit {MAX_BUFFER_BYTES / 1e9:.0f} GB; data rate {p["fps"] * w * h * bpp / 1e6:.0f} MB/s)'
            if exp is not None:
                text += f'; exposure {exp:g} ms (frame period {1000 / p["fps"]:.1f} ms)'
            self.label_info.setText(text)
        except Exception:
            self.label_info.setText('')

    # --- running
    def _check_ready(self):
        if self.running or self.mpc.acquisition_running():
            self._warn('Single measurement', 'Another acquisition is still running or stopping. Please wait until it has ended.')
            return None
        saving_dir = self.lineEdit_savingDir.text().strip()
        if not saving_dir or not os.path.isdir(saving_dir):
            self._warn('Saving directory', 'Please choose an existing saving directory.\n' + saving_dir)
            return None
        return saving_dir

    def start_capture(self):
        names = self.selected_channels()
        if not names:
            self._warn('Single capture', 'Check at least one channel.')
            return
        saving_dir = self._check_ready()
        if saving_dir is None:
            return
        self._run(CAPTURE_JOB.format(channels=repr(names)), names, saving_dir, 'capture')

    def start_fast(self):
        p = self._fast_parameters()
        if not p['channel']:
            self._warn('Fast timelapse', 'No channel available.')
            return
        saving_dir = self._check_ready()
        if saving_dir is None:
            return
        cfg = self._channel_config(p['channel'])
        exp = p['exposure'] if p['exposure'] is not None else (cfg.exposure_time if cfg is not None else None)
        if exp is not None and exp >= 1000.0 / p['fps']:
            self._warn('Fast timelapse', f'The exposure ({exp:g} ms) must be shorter than the frame period ({1000 / p["fps"]:.1f} ms at {p["fps"]:g} frames/s).\n'
                                         f'Use a shorter exposure (tick "exposure" to set it for this movie only) or fewer frames/s.')
            return
        w, h, bpp = self._frame_geometry()
        n = max(1, int(round(p['fps'] * p['duration'])))
        if w and h and n * w * h * bpp > MAX_BUFFER_BYTES:
            self._warn('Fast timelapse', f'{n} frames of {w} x {h} pixels need {n * w * h * bpp / 1e9:.1f} GB of memory, more than the allowed {MAX_BUFFER_BYTES / 1e9:.0f} GB.\n'
                                         f'Use a shorter duration, a smaller region or 8 bit.')
            return
        job = FAST_JOB.format(channel=p['channel'], fps=p['fps'], duration=p['duration'], roi=p['roi'], bits=p['bits'], exposure=p['exposure'], gain=p['gain'],
                              compression=p['compression'])
        self._run(job, [p['channel']], saving_dir, 'fast timelapse')

    def _run(self, job_text, channel_names, saving_dir, kind):
        try:
            os.makedirs(os.path.dirname(JOB_FILE), exist_ok=True)
            with open(JOB_FILE, 'w', encoding='utf-8') as f:
                f.write(job_text)
            nav = self.mpc.navigationController
            pts = make_points(['stage'], [(nav.x_pos_mm, nav.y_pos_mm, nav.z_pos_mm)])
            res = dry_run(JOB_FILE, pts, self._channel_infos())
            if res.status != 'completed':
                self._say(f'The {kind} cannot be started: ' + (res.error or res.status))
                return
        except Exception:
            self._say('Internal error while preparing the measurement:\n' + traceback.format_exc())
            return
        from control.acq_summary import confirm_exposure_lag
        if not confirm_exposure_lag(self, self.mpc, channel_names):
            return
        self._save_cache()
        self.mpc.set_base_path(saving_dir)
        self.mpc.set_selected_configurations(channel_names)
        self.mpc.set_gen_focus_map_flag(False)
        self.mpc.start_new_experiment(self.lineEdit_experimentID.text().strip() or 'single')
        self.mpc.job_owner = 'single'                  # the JOBs tab ignores the messages of this run
        self.running, self._kind = True, kind
        self._set_controls_enabled(False)
        self.btn_abort.setEnabled(True)
        self.signal_acquisition_started.emit(True)
        self.label_status.setText(f'{kind.capitalize()} running ...')
        self.append_log(f'--- {kind} started, experiment folder {self.mpc.experiment_ID} ---')
        self.mpc.run_job(JOB_FILE, pts, save_as=self.combo_save_as.currentData())

    def abort(self):
        if not self.running:
            return
        self.append_log('abort requested ...')
        self.btn_abort.setEnabled(False)
        self.mpc.request_abort_aquisition()

    def _on_job_message(self, text):
        if getattr(self.mpc, 'job_owner', None) == 'single':
            self.append_log(text)

    def acquisition_is_finished(self):
        if not self.running:
            return          # another tab's acquisition finished
        self.running = False
        self.btn_abort.setEnabled(False)
        self._set_controls_enabled(True)
        self.signal_acquisition_started.emit(False)
        res = self.mpc.job_result
        if res is not None:
            self.append_log(f'--- {self._kind} finished ---\n' + describe(res))
            self.label_status.setText(f'{(self._kind or "measurement").capitalize()} {res.status}' + ((': ' + res.error.splitlines()[0]) if res.error else '') +
                                      (f' - {len(res.warnings)} warning(s), see the log' if res.warnings else ''))
        else:
            self.label_status.setText('Measurement ended')

    def _set_controls_enabled(self, enabled):
        for w in (self.lineEdit_savingDir, self.btn_savingDir, self.lineEdit_experimentID, self.combo_save_as, self.list_channels, self.btn_all, self.btn_none,
                  self.btn_capture, self.btn_fast, self.combo_fast_channel, self.spin_fps, self.spin_duration, self.spin_roi, self.combo_bits,
                  self.check_exposure, self.spin_exposure, self.check_gain, self.spin_gain, self.combo_compression):
            w.setEnabled(enabled)
