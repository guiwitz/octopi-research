"""Dry-run backend: pretends to be the microscope, with simulated time, to check a job before it is started.

It reports what the job would do (images per channel/group, autofocus count, estimated duration, late time points).
Images are small dummy arrays, so jobs that decide on image CONTENT may behave differently than on the microscope.
"""
import numpy as np

from control.jobs.api import Backend, SimClock


class DryRunBackend(Backend):
    def __init__(self, channel_infos, positions=None, move_s=1.0, capture_overhead_s=0.4, autofocus_s=6.0, clock=None):
        """channel_infos: {channel name: dict(exposure_ms, gain, intensity)}; positions: list of (x, y, z)"""
        self._infos = dict(channel_infos)
        self.clock = clock or SimClock()
        self._pos = (0.0, 0.0, 0.0)
        self._move_s, self._cap_s, self._af_s = move_s, capture_overhead_s, autofocus_s
        self.messages = []

    def channels(self):
        return list(self._infos)

    def channel_info(self, name):
        return dict(self._infos[name])

    def move_to(self, x, y, z):
        self.clock.sleep(self._move_s)
        self._pos = (x, y, z if z is not None else self._pos[2])

    def move_z(self, z):
        self.clock.sleep(0.15)
        self._pos = (self._pos[0], self._pos[1], z)

    def position(self):
        return self._pos

    def capture(self, channel):
        info = dict(self._infos[channel])
        self.clock.sleep(self._cap_s + info.get('exposure_ms', 0) / 1000.0)
        info.update(x_mm=self._pos[0], y_mm=self._pos[1], z_mm=self._pos[2])
        return np.zeros((8, 8), dtype=np.uint16), info

    def fast_movie(self, channel, n_frames, fps, bits=None, roi=None, exposure_ms=None, gain=None):
        info = dict(self._infos[channel])
        self.clock.sleep(self._cap_s + n_frames / float(fps))
        dtype = np.uint8 if bits == 8 else np.uint16
        frames = np.full((n_frames, 8, 8), 128 if bits == 8 else 20000, dtype=dtype)
        return dict(frames=frames, frame_ids=list(range(n_frames)), timestamps_s=[i / float(fps) for i in range(n_frames)], fps_set=float(fps),
                    exposure_ms=exposure_ms if exposure_ms is not None else info.get('exposure_ms'), gain=gain if gain is not None else info.get('gain', ''),
                    intensity=info.get('intensity', ''), pixel_format='MONO8' if bits == 8 else 'MONO16', incomplete=0, timeouts=0, aborted=False, error=None,
                    notes=[], restore_problems=[], x_mm=self._pos[0], y_mm=self._pos[1], z_mm=self._pos[2])

    def autofocus(self, channel=None, focus_measure=None):
        self.clock.sleep(self._af_s)
        return self._pos[2]

    def save_image(self, image, path, channel):
        pass

    def log(self, message):
        self.messages.append(message)
