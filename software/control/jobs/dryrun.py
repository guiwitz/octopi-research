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

    def position(self):
        return self._pos

    def capture(self, channel):
        info = dict(self._infos[channel])
        self.clock.sleep(self._cap_s + info.get('exposure_ms', 0) / 1000.0)
        info.update(x_mm=self._pos[0], y_mm=self._pos[1], z_mm=self._pos[2])
        return np.zeros((8, 8), dtype=np.uint16), info

    def autofocus(self, channel=None, focus_measure=None):
        self.clock.sleep(self._af_s)
        return self._pos[2]

    def save_image(self, image, path, channel):
        pass

    def log(self, message):
        self.messages.append(message)
