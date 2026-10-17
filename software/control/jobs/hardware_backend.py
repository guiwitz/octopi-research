"""Hardware backend of JOBs: drives the real microscope through the SAME controllers and the SAME call
sequences as the standard multipoint acquisition (MultiPointWorker in control/core.py).

Nothing here talks to the microcontroller or camera in a new way; each method mirrors an existing code block
(noted in the docstrings), so behaviour and safety handling (backlash, z lift for long moves, stabilisation
times, illumination on/off) are the same as in the standard acquisition. Runs in the MultiPointWorker thread.
"""
import time

import numpy as np

import control.utils as utils
from control._def import (TriggerMode, SCAN_STABILIZATION_TIME_MS_Y, SCAN_STABILIZATION_TIME_MS_Z,
                          ENABLE_NL5, NL5_USE_DOUT, MULTIPOINT_BF_SAVING_OPTION)
from control.jobs.api import Backend, JobError

LONG_MOVE_MM = 13           # as in the standard acquisition: for jumps longer than this the objective is lifted first
SAFE_Z_MM = 2


class HardwareBackend(Backend):
    def __init__(self, worker):
        self.w = worker
        self.camera = worker.camera
        self.live = worker.liveController
        self.nav = worker.navigationController
        self.mcu = worker.microcontroller
        self.af = worker.autofocusController
        self.configs = {c.name: c for c in worker.configurationManager.configurations}
        self._last_xy = None
        self._unsupported = ('RGB', 'USB Spectrometer')

    # --- info
    def channels(self):
        return [n for n in self.configs if not any(u in n for u in self._unsupported)]

    def channel_info(self, name):
        c = self.configs[name]
        return dict(exposure_ms=c.exposure_time, gain=c.analog_gain, intensity=c.illumination_intensity)

    def position(self):
        return (self.nav.x_pos_mm, self.nav.y_pos_mm, self.nav.z_pos_mm)

    def abort_requested(self):
        return bool(self.w.multiPointController.abort_acqusition_requested)

    def log(self, message):
        print('[job] ' + message)
        try:
            self.w.signal_job_message.emit(message)
        except Exception:
            pass

    # --- stage: mirrors MultiPointWorker.run_single_time_point, 'move to the specified coordinate'
    def move_to(self, x, y, z):
        nav, w = self.nav, self.w
        if self._last_xy is None:
            distance = 14                      # first move of a job: always lift first (as the standard acquisition does)
        else:
            distance = float(np.hypot(x - self._last_xy[0], y - self._last_xy[1]))
        if distance > LONG_MOVE_MM:
            if z is not None and z >= nav.z_pos_mm:
                nav.move_z_to(SAFE_Z_MM)
                w.wait_till_operation_is_completed()
            else:
                nav.move_z_to(SAFE_Z_MM)
                w.wait_till_operation_is_completed()
                self._clear_z_backlash()
        nav.move_x_to(x)
        nav.move_y_to(y)
        w.wait_till_operation_is_completed()
        if z is not None:
            if z >= nav.z_pos_mm:
                nav.move_z_to(z)
                w.wait_till_operation_is_completed()
            else:
                nav.move_z_to(z)
                w.wait_till_operation_is_completed()
                if nav.get_pid_control_flag(2) is False:
                    self._clear_z_backlash()
        else:
            w.wait_till_operation_is_completed()
        time.sleep(SCAN_STABILIZATION_TIME_MS_Y / 1000)
        if z is not None:
            time.sleep(SCAN_STABILIZATION_TIME_MS_Z / 1000)
        self._last_xy = (x, y)

    def _clear_z_backlash(self):
        steps = max(160, 20 * self.nav.z_microstepping)
        self.nav.move_z_usteps(-steps)
        self.w.wait_till_operation_is_completed()
        self.nav.move_z_usteps(steps)
        self.w.wait_till_operation_is_completed()

    # --- autofocus: mirrors the contrast-AF block of run_single_time_point
    def autofocus(self, channel=None):
        name = channel or self.af.af_channel
        config = self._config(name)
        self.w.signal_current_configuration.emit(config)        # blocking: GUI applies the AF channel
        self.af.autofocus()
        self.af.wait_till_autofocus_has_completed()
        return self.nav.z_pos_mm

    # --- imaging: mirrors the single-channel block of run_single_time_point
    def capture(self, channel):
        w = self.w
        config = self._config(channel)
        w.signal_current_configuration.emit(config)             # blocking: GUI applies exposure, gain, illumination, filter
        w.wait_till_operation_is_completed()
        image = None
        for _ in range(3):
            image = self._trigger_and_read(config)
            if image is not None:
                break
            time.sleep(0.1)
        if image is None:
            raise JobError(f'camera returned no image for channel {channel!r} (3 attempts)')
        # process the image
        image = utils.crop_image(image, w.crop_width, w.crop_height)
        image = utils.rotate_and_flip_image(image, rotate_image_angle=self.camera.rotate_image_angle, flip_image=self.camera.flip_image)
        shown = utils.crop_image(image, round(w.crop_width * w.display_resolution_scaling), round(w.crop_height * w.display_resolution_scaling))
        w.image_to_display.emit(shown)
        w.image_to_display_multi.emit(shown, config.illumination_source)
        if self.camera.is_color and 'BF LED matrix' in config.name:
            import cv2
            if MULTIPOINT_BF_SAVING_OPTION == 'RGB2GRAY':
                image = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
            elif MULTIPOINT_BF_SAVING_OPTION == 'Green Channel Only':
                image = image[:, :, 1]
        x, y, z = self.position()
        info = dict(exposure_ms=config.exposure_time, gain=config.analog_gain, intensity=config.illumination_intensity,
                    x_mm=x, y_mm=y, z_mm=z)
        return image, info

    def _trigger_and_read(self, config):
        w, cam, live = self.w, self.camera, self.live
        if live.trigger_mode == TriggerMode.SOFTWARE:
            try:
                live.turn_on_illumination()
                w.wait_till_operation_is_completed()
                cam.send_trigger()
                return cam.read_frame()
            finally:
                live.turn_off_illumination()
        if live.trigger_mode == TriggerMode.HARDWARE:
            if 'Fluorescence' in config.name and ENABLE_NL5 and NL5_USE_DOUT:
                cam.image_is_ready = False
                w.microscope.nl5.start_acquisition()
                return cam.read_frame(reset_image_ready_flag=False)
            self.mcu.send_hardware_trigger(control_illumination=True, illumination_on_time_us=cam.exposure_time * 1000)
            return cam.read_frame()
        raise JobError('JOBs support the software and the hardware trigger mode, not continuous mode')

    def save_image(self, image, path, channel):
        from control.towbin_funs import save_single_plane_tiff
        save_single_plane_tiff(image, path, channels=[channel])

    def _config(self, name):
        if name not in self.configs:
            raise JobError(f'unknown channel {name!r}')
        if any(u in name for u in self._unsupported):
            raise JobError(f'channel {name!r} is not supported by JOBs yet')
        return self.configs[name]

    # --- cleanup: illumination off, joystick button enabled again (as at the end of a standard time point)
    def cleanup(self):
        try:
            self.live.turn_off_illumination()
        finally:
            self.nav.enable_joystick_button_action = True
