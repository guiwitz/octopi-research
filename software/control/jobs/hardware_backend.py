"""Hardware backend of JOBs: drives the real microscope through the SAME controllers and the SAME call
sequences as the standard multipoint acquisition (MultiPointWorker in control/core.py).

Nothing here talks to the microcontroller or camera in a new way; each method mirrors an existing code block
(noted in the docstrings), so behaviour and safety handling (backlash, z lift for long moves, stabilisation
times, illumination on/off) are the same as in the standard acquisition. Runs in the MultiPointWorker thread.
"""
import os
import time

import numpy as np

import control.utils as utils
from control._def import (TriggerMode, SCAN_STABILIZATION_TIME_MS_Y, SCAN_STABILIZATION_TIME_MS_Z,
                          ENABLE_NL5, NL5_USE_DOUT, MULTIPOINT_BF_SAVING_OPTION)
from control.jobs.api import Backend, JobError

def usbfs_memory_mb():
    """Linux USB buffer limit in MB (None if unknown): the FLIR driver needs a large one for fast recordings"""
    try:
        with open('/sys/module/usbcore/parameters/usbfs_memory_mb') as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


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

    # --- pause / resume (flags live in the MultiPointController, set from the GUI thread)
    def pause_requested(self):
        return bool(getattr(self.w.multiPointController, 'job_pause_requested', False))

    def take_new_positions(self):
        mpc = self.w.multiPointController
        new = getattr(mpc, 'job_new_positions', None)
        mpc.job_new_positions = None
        return new

    def on_pause(self):
        self._joystick(True)          # the stage may be moved by hand while paused
        try:
            self.w.signal_job_message.emit('job paused: you can now change the positions and move the stage; press "Resume job" to continue')
        except Exception:
            pass

    def on_resume(self):
        # the user may have moved the stage and used live view meanwhile: stop live, and force the safe z-lift
        # (as for the first move of a job) before the next stage move
        try:
            if self.live.is_live:
                self.live.stop_live()
        except Exception:
            pass
        self._last_xy = None
        self._joystick(False)

    def _joystick(self, enabled):
        try:
            from control.core import LOCK_JOYSTICK_DURING_ACQUISITION
            if LOCK_JOYSTICK_DURING_ACQUISITION:
                self.mcu.set_joystick_enabled(enabled)
                self.w.wait_till_operation_is_completed()
        except Exception as e:
            print('joystick lock not available: ' + str(e))

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

    def move_z(self, z):
        """z-only absolute move (mirrors the z part of move_to: backlash cleared after moving down, stabilisation time)."""
        nav, w = self.nav, self.w
        going_down = z < nav.z_pos_mm
        nav.move_z_to(z)
        w.wait_till_operation_is_completed()
        if going_down and nav.get_pid_control_flag(2) is False:
            self._clear_z_backlash()
        time.sleep(SCAN_STABILIZATION_TIME_MS_Z / 1000)

    def channel_z_offset_um(self, name):
        """Z offset (um) of the channel from the channel configuration; the standard acquisition applies it, z-stacks do too."""
        return float(self._config(name).z_offset or 0.0)

    def pixel_size_um(self):
        from control._def import CAMERA_PIXEL_SIZE_UM, CAMERA_SENSOR
        return CAMERA_PIXEL_SIZE_UM.get(CAMERA_SENSOR)

    def _clear_z_backlash(self):
        steps = max(160, 20 * self.nav.z_microstepping)
        self.nav.move_z_usteps(-steps)
        self.w.wait_till_operation_is_completed()
        self.nav.move_z_usteps(steps)
        self.w.wait_till_operation_is_completed()

    # --- autofocus: mirrors the contrast-AF block of run_single_time_point
    def autofocus(self, channel=None, focus_measure=None):
        name = channel or self.af.af_channel
        config = self._config(name)
        self.w.signal_current_configuration.emit(config)        # blocking: GUI applies the AF channel
        previous = self.af.focus_measure
        if focus_measure is not None:
            from control import autofocus_settings
            reg = autofocus_settings.REGISTRY
            if '::' in focus_measure and not reg.is_custom(focus_measure):      # "file.py::function" (relative to the software folder)
                path, func = focus_measure.rsplit('::', 1)
                if not os.path.isabs(path):
                    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), path)
                focus_measure = reg.make_key(path, func)
            if reg.is_custom(focus_measure):
                try:
                    reg.get(focus_measure)                                       # loads the file if needed
                except Exception as e:
                    raise JobError(f'focus measure {focus_measure!r} not usable: {e}')
            elif focus_measure not in reg.keys():
                raise JobError(f'unknown focus measure {focus_measure!r}; available: {reg.keys()}')
            self.af.focus_measure = focus_measure                # attribute only (no GUI signal); restored below
        try:
            self.af.autofocus()
            self.af.wait_till_autofocus_has_completed()
        finally:
            self.af.focus_measure = previous
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

    # --- video-rate movie: the camera runs freely (Camera.fast_acquire), the light is on only while it records
    def fast_movie(self, channel, n_frames, fps, bits=None, roi=None, exposure_ms=None, gain=None):
        from control.jobs.api import MAX_BUFFER_BYTES
        w, cam, live = self.w, self.camera, self.live
        config = self._config(channel)
        w.signal_current_configuration.emit(config)             # blocking: GUI applies exposure, gain, illumination source, filter
        w.wait_till_operation_is_completed()
        notes = []
        mb = usbfs_memory_mb()
        if mb is not None and mb < 200:
            notes.append(f'the USB buffer limit of this computer is only {mb} MB (usbfs_memory_mb); if frames are lost, raise it to 1000 '
                         f'(GRUB: usbcore.usbfs_memory_mb=1000)')
        step = max(1, int(round(fps / 5.0)))            # about 5 pictures per second in the live display

        def show(i, img):
            if i % step == 0:
                small = img[::2, ::2]
                shown = (small.astype(np.uint16) << 8) if small.dtype == np.uint8 else small
                w.image_to_display.emit(np.ascontiguousarray(shown))

        def light_on(_info=None):
            live.turn_on_illumination()
            w.wait_till_operation_is_completed()
        x, y, z = self.position()
        try:
            if hasattr(cam, 'fast_acquire'):
                try:
                    res = cam.fast_acquire(n_frames, fps, bits=bits, roi=roi, exposure_ms=exposure_ms, gain=gain, should_abort=self.abort_requested,
                                           on_frame=show, max_bytes=MAX_BUFFER_BYTES, on_started=light_on, on_stopped=live.turn_off_illumination)
                except ValueError as e:
                    raise JobError(str(e))
                ts = [t / 1e9 for t in res['timestamps_ns']]
            else:
                res, ts = self._generic_fast_movie(config, n_frames, fps, show, notes), None
                ts = res.pop('timestamps_s')
        finally:
            live.turn_off_illumination()
        frames = res['frames']
        if frames is not None and len(frames):
            ang, flip = self.camera.rotate_image_angle, self.camera.flip_image
            if ang or flip:                         # as for single images
                frames = np.stack([utils.rotate_and_flip_image(f, rotate_image_angle=ang, flip_image=flip) for f in frames])
        res.update(frames=frames, timestamps_s=ts, notes=notes + res.get('notes', []), intensity=config.illumination_intensity, x_mm=x, y_mm=y, z_mm=z)
        return res

    def _generic_fast_movie(self, config, n_frames, fps, show, notes):
        """Cameras without a fast mode (simulation, other makes): software-triggered frames paced in software. Slower, with irregular timing."""
        notes.append('this camera has no fast mode: frames are software-triggered, the frame rate and its regularity are limited')
        frames, ts, t0 = [], [], time.time()
        for i in range(n_frames):
            if self.abort_requested():
                break
            wait = t0 + i / float(fps) - time.time()
            if wait > 0:
                time.sleep(wait)
            img = self._trigger_and_read(config)
            if img is None:
                continue
            frames.append(img)
            ts.append(time.time() - t0)
            show(len(frames) - 1, img)
        return dict(frames=np.stack(frames) if frames else None, frame_ids=list(range(len(frames))), timestamps_s=ts, fps_set=None,
                    exposure_ms=config.exposure_time, gain=config.analog_gain, pixel_format=str(getattr(self.camera, 'pixel_format', '') or ''),
                    incomplete=0, timeouts=0, aborted=self.abort_requested(), error=None, restore_problems=[])

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
