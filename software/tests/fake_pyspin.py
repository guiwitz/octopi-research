"""A fake of the PySpin (FLIR Spinnaker) module, good enough to run control/camera_flir.py without a camera.

It models what matters for the acquisition code:
  * GenICam nodes (enumerations, floats, integers, booleans, commands, categories) with the names camera_flir uses;
  * acquisition start/stop (BeginAcquisition / EndAcquisition, errors when called twice, as Spinnaker does);
  * trigger modes: continuous (TriggerMode Off), software trigger (TriggerSoftware), hardware trigger (Line*,
    via FakeCamera.hardware_trigger());
  * timing: a frame is ready exposure + readout after the trigger; GetNextImage(timeout) waits for it and raises
    SpinnakerException(-1011) on timeout, like the real SDK;
  * a stream buffer (10 buffers, oldest first) - stale frames stay in it until read or until acquisition stops;
  * image event handlers (callbacks) called from a separate thread;
  * fault injection (drop a trigger, deliver an incomplete frame) and a log of everything;
  * the Sequencer (Blackfly S): sets with their own ExposureTime and Gain, saved with SequencerSetSave, advanced on
    each frame start (SequencerTriggerSource FrameStart); FakeCamera(sequencer=False) models a camera without it.
Each image carries its own description: pixel [0,0] = frame id (mod 65536), [0,1] = exposure in ms (rounded),
[0,2] = trigger number; the rest is filled with a value proportional to the exposure.

Usage in a test:  import tests.fake_pyspin as fake; sys.modules['PySpin'] = fake; import control.camera_flir
"""
import threading
import time

import numpy as np

# ---------------------------------------------------------------------------------------------------------------
# constants used by camera_flir
intfIValue, intfIBase, intfIInteger, intfIBoolean, intfICommand, intfIFloat, intfIString, intfIRegister, \
    intfICategory, intfIEnumeration, intfIEnumEntry, intfIPort = range(12)
PixelFormat_Mono8, PixelFormat_Mono10, PixelFormat_Mono12, PixelFormat_Mono16, PixelFormat_RGB8, PixelFormat_RGB16, \
    PixelFormat_BayerRG8, PixelFormat_BayerRG12, PixelFormat_Mono10p, PixelFormat_Mono12p = range(1, 11)
SPINNAKER_COLOR_PROCESSING_ALGORITHM_HQ_LINEAR = 3

SENSOR_W, SENSOR_H = 3072, 2048        # IMX178
READOUT_S = 0.03                       # sensor readout + transfer
BUFFERS = 10


class SpinnakerException(Exception):
    def __init__(self, message, code=-1001):
        super().__init__(f'Spinnaker: {message} [{code}]')
        self.errorcode = code
        self.message = message


# ---------------------------------------------------------------------------------------------------------------
# nodes
class Node:
    iface = intfIBase

    def __init__(self, name, readable=True, writable=True, on_change=None):
        self.name, self.readable, self.writable, self.on_change = name, readable, writable, on_change

    def GetName(self): return self.name
    def GetDisplayName(self): return self.name
    def GetPrincipalInterfaceType(self): return self.iface
    def ToString(self): return str(getattr(self, 'value', ''))

    def _changed(self):
        if self.on_change:
            self.on_change(self)


class ValueNode(Node):
    def __init__(self, name, value, vmin=None, vmax=None, inc=1, **kw):
        super().__init__(name, **kw)
        self.value, self.vmin, self.vmax, self.inc = value, vmin, vmax, inc

    def GetValue(self):
        if not self.readable:
            raise SpinnakerException(f'node {self.name} not readable', -2006)
        return self.value

    def SetValue(self, v, *a):
        if not self.writable:
            raise SpinnakerException(f'node {self.name} not writable', -2006)
        vmax = self.GetMax()
        if self.vmin is not None and v < self.vmin or vmax is not None and v > vmax:
            raise SpinnakerException(f'{self.name}: value {v} out of range [{self.vmin}, {self.vmax}]', -2002)
        self.value = type(self.value)(v) if not isinstance(self.value, bool) else bool(v)
        self._changed()

    def GetMin(self): return self.vmin
    def GetMax(self): return self.vmax() if callable(self.vmax) else self.vmax
    def GetInc(self): return self.inc


class IntegerNode(ValueNode):
    iface = intfIInteger


class FloatNode(ValueNode):
    iface = intfIFloat


class BooleanNode(ValueNode):
    iface = intfIBoolean


class StringNode(ValueNode):
    iface = intfIString


class CommandNode(Node):
    iface = intfICommand

    def __init__(self, name, action, **kw):
        super().__init__(name, **kw)
        self.action = action

    def Execute(self):
        if not self.writable:
            raise SpinnakerException(f'command {self.name} not executable', -2006)
        self.action()

    def IsDone(self): return True


class EnumEntry(Node):
    iface = intfIEnumEntry

    def __init__(self, name, value):
        super().__init__(name)
        self.value, self.symbolic = value, name

    def GetValue(self): return self.value
    def GetSymbolic(self): return self.symbolic


class EnumNode(Node):
    iface = intfIEnumeration

    def __init__(self, name, entries, current, **kw):
        super().__init__(name, **kw)
        self.entries = {n: EnumEntry(n, i) for i, n in enumerate(entries)}
        self.current = self.entries[current].value

    def GetEntryByName(self, name): return self.entries.get(name)
    def GetEntries(self): return list(self.entries.values())
    def GetIntValue(self): return self.current

    def GetCurrentEntry(self):
        return next(e for e in self.entries.values() if e.value == self.current)

    def SetIntValue(self, v):
        if not self.writable:
            raise SpinnakerException(f'node {self.name} not writable', -2006)
        if v not in [e.value for e in self.entries.values()]:
            raise SpinnakerException(f'{self.name}: invalid entry {v}', -2002)
        self.current = v
        self._changed()

    @property
    def symbolic(self):
        return self.GetCurrentEntry().symbolic

    def ToString(self): return self.symbolic


class CategoryNode(Node):
    iface = intfICategory

    def __init__(self, name, features=()):
        super().__init__(name)
        self.features = list(features)

    def GetFeatures(self): return list(self.features)


class NodeMap:
    def __init__(self, nodes):
        self.nodes = {n.name: n for n in nodes}

    def GetNode(self, name): return self.nodes.get(name)
    def add(self, node): self.nodes[node.name] = node


def _ptr(node):
    return node


CValuePtr = CStringPtr = CIntegerPtr = CFloatPtr = CBooleanPtr = CCommandPtr = CEnumerationPtr = CEnumEntryPtr = \
    CCategoryPtr = _ptr


def IsAvailable(node): return node is not None
def IsImplemented(node): return node is not None
def IsReadable(node): return node is not None and getattr(node, 'readable', True)
def IsWritable(node): return node is not None and getattr(node, 'writable', True)


# ---------------------------------------------------------------------------------------------------------------
# images
class ImageEventHandler:
    def __init__(self, *a, **k):
        pass


class FakeImage:
    def __init__(self, data, frame_id, incomplete=False, meta=None):
        self.data, self.frame_id, self.incomplete, self.meta = data, frame_id, incomplete, meta or {}
        self.released = False

    def IsIncomplete(self): return self.incomplete
    def GetImageStatus(self): return 7 if self.incomplete else 0
    def GetFrameID(self): return self.frame_id
    def GetWidth(self): return self.data.shape[1]
    def GetHeight(self): return self.data.shape[0]
    def GetTimeStamp(self): return int(self.meta.get('t', 0) * 1e9)

    def GetNDArray(self):
        if self.released:
            raise SpinnakerException('image already released', -1006)
        return self.data.copy()

    def Release(self):
        self.released = True

    def GetChunkData(self):
        meta = self.meta
        return type('ChunkData', (), dict(GetExposureTime=lambda s: meta.get('exposure_ms', 0) * 1000.0,
                                          GetFrameID=lambda s: meta.get('frame_id', 0),
                                          GetTimestamp=lambda s: int(meta.get('t', 0) * 1e9),
                                          GetGain=lambda s: meta.get('gain', 0.0),
                                          GetSequencerSetActive=lambda s: meta.get('sequencer_set', 0)))()


class ImageProcessor:
    def SetColorProcessing(self, algo): pass

    def Convert(self, image, fmt):
        d = image.data
        if fmt == PixelFormat_Mono8 and d.dtype != np.uint8:
            d = (d >> 8).astype(np.uint8)
        return FakeImage(d, image.frame_id, image.incomplete, image.meta)


# ---------------------------------------------------------------------------------------------------------------
# the camera
class FakeCamera:
    SEQUENCER_SETS = 8

    def __init__(self, serial='FAKE178', model='Blackfly S BFS-U3-63S4M', sequencer=True):
        self.serial, self.model = serial, model
        self.has_sequencer = sequencer
        self.seq_sets = {}         # set number -> dict(exposure_us, gain, next, trigger_source)
        self.seq_active = 0
        self.initialized = False
        self.streaming = False
        self.handler = None
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.buffer = []          # ready FakeImages (oldest first)
        self.pending = []         # (ready_time, meta)
        self.frame_id = 0
        self.n_triggers = 0
        self.log = []             # (time, event, details)
        self.fault = dict(drop_triggers=0, incomplete_frames=0)
        self.dropped_full_buffer = 0
        self.exposure_lag = False   # True: like the real Blackfly S: an exposure written now is used from the SECOND trigger on (the first uses the one latched before)
        self._latched = None
        self.paced = False         # True: free running at AcquisitionFrameRate (when enabled), else ~30 fps as before
        self.concurrent_calls = 0
        self._inside = 0
        self._thread = None
        self._stop = False
        self._last_continuous = 0.0
        n = []
        self.n_trigger_mode = EnumNode('TriggerMode', ['Off', 'On'], 'Off')
        self.n_trigger_source = EnumNode('TriggerSource', ['Software', 'Line0', 'Line1', 'Line2', 'Line3'], 'Software')
        self.n_trigger_activation = EnumNode('TriggerActivation', ['RisingEdge', 'FallingEdge'], 'RisingEdge')
        self.n_exposure = FloatNode('ExposureTime', 10000.0, 6.0, 30e6)
        self.n_exposure_auto = EnumNode('ExposureAuto', ['Off', 'Once', 'Continuous'], 'Continuous')
        self.n_gain = FloatNode('Gain', 0.0, 0.0, 47.9)
        self.n_gain_auto = EnumNode('GainAuto', ['Off', 'Once', 'Continuous'], 'Continuous')
        self.n_pixel_format = EnumNode('PixelFormat', ['Mono8', 'Mono10', 'Mono10p', 'Mono12', 'Mono12p', 'Mono16'], 'Mono8')
        self.n_adc = EnumNode('AdcBitDepth', ['Bit10', 'Bit12', 'Bit14'], 'Bit10')
        self.n_width = IntegerNode('Width', SENSOR_W, 16, SENSOR_W, 16)
        self.n_height = IntegerNode('Height', SENSOR_H, 16, SENSOR_H, 2)
        self.n_offx = IntegerNode('OffsetX', 0, 0, SENSOR_W - 16, 4)
        self.n_offy = IntegerNode('OffsetY', 0, 0, SENSOR_H - 16, 2)
        for node in (self.n_width, self.n_height):
            node.on_change = self._roi_changed
        self.n_trigger_software = CommandNode('TriggerSoftware', self._software_trigger)
        n += [self.n_trigger_mode, self.n_trigger_source, self.n_trigger_activation, self.n_exposure, self.n_exposure_auto,
              self.n_gain, self.n_gain_auto, self.n_pixel_format, self.n_adc, self.n_width, self.n_height, self.n_offx,
              self.n_offy, self.n_trigger_software,
              IntegerNode('WidthMax', SENSOR_W, writable=False), IntegerNode('HeightMax', SENSOR_H, writable=False),
              IntegerNode('SensorWidth', SENSOR_W, writable=False), IntegerNode('SensorHeight', SENSOR_H, writable=False),
              BooleanNode('AcquisitionFrameRateEnable', False), FloatNode('AcquisitionFrameRate', 30.0, 1.0, self._max_frame_rate),
              IntegerNode('DeviceLinkThroughputLimit', 380000000, 10000000, 380000000),
              BooleanNode('GammaEnable', True), BooleanNode('ReverseX', False), BooleanNode('ReverseY', False),
              EnumNode('LineSelector', ['Line0', 'Line1', 'Line2', 'Line3'], 'Line0'),
              EnumNode('CounterEventSource', ['Off', 'Line2'], 'Off'), CommandNode('CounterReset', lambda: None),
              BooleanNode('ChunkModeActive', False),
              EnumNode('ChunkSelector', ['Image', 'FrameID', 'ExposureTime', 'Gain', 'Timestamp']
                       + (['SequencerSetActive'] if sequencer else []), 'Image'),
              BooleanNode('ChunkEnable', False),
              EnumNode('ExposureMode', ['Timed', 'TriggerWidth'], 'Timed')]
        if sequencer:
            n += self._sequencer_nodes()
        self.nodemap = NodeMap(n)
        self.nodemap.add(CategoryNode('Root', []))
        info = CategoryNode('DeviceInformation', [StringNode('DeviceModelName', model, writable=False),
                                                  StringNode('DeviceSerialNumber', serial, writable=False)])
        self.tl_device = NodeMap([CategoryNode('Root', [info])])
        self.tl_stream = NodeMap([CategoryNode('Root', [])])

    # --- sequencer (modelled on the Blackfly S / Spinnaker Sequencer example)
    def _sequencer_nodes(self):
        self.n_seq_mode = EnumNode('SequencerMode', ['Off', 'On'], 'Off', on_change=self._seq_mode_changed)
        self.n_seq_config = EnumNode('SequencerConfigurationMode', ['Off', 'On'], 'Off', on_change=self._seq_config_changed)
        self.n_seq_valid = EnumNode('SequencerConfigurationValid', ['No', 'Yes'], 'No', writable=False)
        self.n_seq_set_valid = EnumNode('SequencerSetValid', ['No', 'Yes'], 'No', writable=False)
        self.n_seq_selector = IntegerNode('SequencerSetSelector', 0, 0, self.SEQUENCER_SETS - 1, writable=False,
                                          on_change=self._seq_selector_changed)
        self.n_seq_start = IntegerNode('SequencerSetStart', 0, 0, self.SEQUENCER_SETS - 1, writable=False)
        self.n_seq_next = IntegerNode('SequencerSetNext', 0, 0, self.SEQUENCER_SETS - 1, writable=False)
        self.n_seq_path = IntegerNode('SequencerPathSelector', 0, 0, 1, writable=False)
        self.n_seq_trigger = EnumNode('SequencerTriggerSource', ['Off', 'FrameStart'], 'Off', writable=False)
        self.n_seq_active = IntegerNode('SequencerSetActive', 0, 0, self.SEQUENCER_SETS - 1, writable=False)
        self.n_seq_feature = EnumNode('SequencerFeatureSelector', ['ExposureTime', 'Gain'], 'ExposureTime')
        self.n_seq_feature_enable = BooleanNode('SequencerFeatureEnable', True, writable=False)
        self.n_seq_save = CommandNode('SequencerSetSave', self._seq_save, writable=False)
        self.n_seq_load = CommandNode('SequencerSetLoad', self._seq_load, writable=False)
        self._seq_config_nodes = [self.n_seq_selector, self.n_seq_start, self.n_seq_next, self.n_seq_path,
                                  self.n_seq_trigger, self.n_seq_save, self.n_seq_load]
        return [self.n_seq_mode, self.n_seq_config, self.n_seq_valid, self.n_seq_set_valid, self.n_seq_selector,
                self.n_seq_start, self.n_seq_next, self.n_seq_path, self.n_seq_trigger, self.n_seq_active,
                self.n_seq_feature, self.n_seq_feature_enable, self.n_seq_save, self.n_seq_load]

    def _seq_validate(self):
        ok, seen, s = False, set(), self.n_seq_start.value
        while s in self.seq_sets and s not in seen and self.seq_sets[s]['trigger_source'] == 'FrameStart':
            seen.add(s)
            s = self.seq_sets[s]['next']
            if s in seen:
                ok = True          # the path from the start set closes into a loop of saved sets
        self.n_seq_valid.current = self.n_seq_valid.entries['Yes' if ok else 'No'].value
        return ok

    def _seq_config_changed(self, node):
        on = node.symbolic == 'On'
        if on and self.n_seq_mode.symbolic == 'On':
            node.current = node.entries['Off'].value
            raise SpinnakerException('SequencerConfigurationMode: switch SequencerMode Off first', -2006)
        for nd in self._seq_config_nodes:
            nd.writable = on
        if not on:
            self._seq_validate()
        self._log('sequencer_configuration', on=on)

    def _seq_mode_changed(self, node):
        on = node.symbolic == 'On'
        if on and (self.n_seq_config.symbolic == 'On' or not self._seq_validate()):
            node.current = node.entries['Off'].value
            raise SpinnakerException('SequencerMode On: configuration mode is on or the configuration is not valid', -2006)
        if on and (self.n_exposure_auto.symbolic != 'Off' or self.n_gain_auto.symbolic != 'Off'):
            node.current = node.entries['Off'].value
            raise SpinnakerException('SequencerMode On: ExposureAuto and GainAuto must be Off', -2006)
        self.seq_active = self.n_seq_start.value
        self.n_seq_active.value = self.seq_active
        for nd in (self.n_exposure, self.n_gain, self.n_exposure_auto, self.n_gain_auto, self.n_seq_config):
            nd.writable = not on       # as on the camera: the set's values rule while the sequencer runs
        self._log('sequencer', on=on, start=self.seq_active)

    def _seq_selector_changed(self, node):
        st = self.seq_sets.get(node.value)
        self.n_seq_set_valid.current = self.n_seq_set_valid.entries['Yes' if st else 'No'].value

    def _seq_save(self):
        self.seq_sets[self.n_seq_selector.value] = dict(exposure_us=self.n_exposure.value, gain=self.n_gain.value,
                                                        next=self.n_seq_next.value, trigger_source=self.n_seq_trigger.symbolic)
        self._seq_selector_changed(self.n_seq_selector)
        self._log('sequencer_save', set=self.n_seq_selector.value, **self.seq_sets[self.n_seq_selector.value])

    def _seq_load(self):
        st = self.seq_sets.get(self.n_seq_selector.value)
        if st is None:
            raise SpinnakerException('SequencerSetLoad: set not saved', -2006)
        self.n_exposure.value, self.n_gain.value, self.n_seq_next.value = st['exposure_us'], st['gain'], st['next']
        self.n_seq_trigger.current = self.n_seq_trigger.entries[st['trigger_source']].value

    def _sequencer_on(self):
        return self.has_sequencer and self.n_seq_mode.symbolic == 'On'

    # --- bookkeeping
    def _log(self, event, **details):
        self.log.append((time.time(), event, details))

    def _enter(self):
        self._inside += 1
        if self._inside > 1:
            self.concurrent_calls += 1

    def _leave(self):
        self._inside -= 1

    def _roi_changed(self, node):
        if self.streaming:
            raise SpinnakerException('cannot change the image size while streaming', -2006)

    # --- life cycle
    def Init(self):
        self.initialized = True

    def DeInit(self):
        if self.streaming:
            self.EndAcquisition()
        self.initialized = False

    def IsInitialized(self): return self.initialized

    def GetNodeMap(self):
        if not self.initialized:
            raise SpinnakerException('camera not initialized', -1002)
        return self.nodemap

    def GetTLDeviceNodeMap(self): return self.tl_device
    def GetTLStreamNodeMap(self): return self.tl_stream

    def BeginAcquisition(self):
        with self.lock:
            if not self.initialized:
                raise SpinnakerException('camera not initialized', -1002)
            if self.streaming:
                raise SpinnakerException('Camera is already streaming', -1002)
            self.streaming = True
            self.buffer, self.pending = [], []
            if self._sequencer_on():
                self.seq_active = self.n_seq_start.value
                self.n_seq_active.value = self.seq_active
            self._last_continuous = time.time()
            for nd in (self.n_width, self.n_height, self.n_pixel_format):
                nd.writable = False        # as on a real camera: not writable while acquiring
            self._stop = False
            self._thread = threading.Thread(target=self._sensor_loop, daemon=True)
            self._thread.start()
            self._log('begin')

    def EndAcquisition(self):
        with self.lock:
            if not self.streaming:
                raise SpinnakerException('Camera is not started', -1002)
            self.streaming = False
            self._stop = True
            stale = len(self.buffer) + len(self.pending)
            self.buffer, self.pending = [], []
            for nd in (self.n_width, self.n_height, self.n_pixel_format):
                nd.writable = True
            self.cond.notify_all()
            self._log('end', discarded=stale)
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(1.0)

    def IsStreaming(self): return self.streaming

    def RegisterEventHandler(self, handler):
        with self.lock:
            if self.handler is not None:
                raise SpinnakerException('an event handler is already registered', -1001)
            self.handler = handler
            self._log('register')

    def UnregisterEventHandler(self, handler):
        with self.lock:
            if self.handler is None:
                raise SpinnakerException('no event handler registered', -1001)
            self.handler = None
            self._log('unregister')

    # --- triggers
    def sensor_readout_s(self):
        """IMX178-like rolling-shutter readout of the current ROI: 16.8 ms (8 bit) / 33.6 ms (>8 bit ADC) for 2048 rows."""
        full = 1 / 59.6 if self.n_pixel_format.symbolic == 'Mono8' else 2 / 59.6
        return full * self.n_height.value / SENSOR_H

    def _max_frame_rate(self):
        """as on the camera: limited by exposure and by readout (both overlap: the larger one counts)"""
        return 1.0 / max(self.sensor_readout_s(), self.n_exposure.value / 1e6 + 1e-4)

    def _exposure_s(self):
        return self.n_exposure.value / 1e6

    def _trigger(self, source):
        with self.lock:
            if not self.streaming:
                self._log('trigger_ignored', source=source, why='not streaming')
                return
            if self.n_trigger_mode.symbolic != 'On' or self.n_trigger_source.symbolic != source:
                self._log('trigger_ignored', source=source, why='trigger mode ' + self.n_trigger_mode.symbolic + '/' + self.n_trigger_source.symbolic)
                return
            self.n_triggers += 1
            if self.exposure_lag:
                cur = self.n_exposure.value
                use = cur if self._latched is None else self._latched
                self._latched = cur
            else:
                use = self.n_exposure.value
            meta = dict(trigger=self.n_triggers, exposure_ms=use / 1000.0, gain=self.n_gain.value,
                        source=source, t_trigger=time.time())
            if self.fault['drop_triggers'] > 0:
                self.fault['drop_triggers'] -= 1
                self._log('trigger_lost', **meta)
                return
            if self._sequencer_on():       # this frame uses the active set; the next frame start moves to its next set
                st = self.seq_sets[self.seq_active]
                meta.update(exposure_ms=st['exposure_us'] / 1000.0, gain=st['gain'], sequencer_set=self.seq_active)
                self.seq_active = st['next']
                self.n_seq_active.value = self.seq_active
            self.pending.append((time.time() + meta['exposure_ms'] / 1000.0 + READOUT_S, meta))
            self._log('trigger', **meta)

    def _software_trigger(self):
        self._enter()
        try:
            self._trigger('Software')
        finally:
            self._leave()

    def hardware_trigger(self, line='Line2'):
        self._trigger(line)

    # --- sensor: turns pending exposures into images (in the buffer or to the event handler)
    def _make_image(self, meta):
        self.frame_id += 1
        fmt = self.n_pixel_format.symbolic
        h, w = self.n_height.value, self.n_width.value
        if fmt == 'Mono8':
            dtype, vmax = np.uint8, 255
        else:
            dtype, vmax = np.uint16, 65535
        level = min(vmax - 1, int(meta['exposure_ms'] * (vmax / 1000.0)))   # 1000 ms -> full scale
        data = np.full((h, w), level, dtype=dtype)
        data[0, 0] = self.frame_id % (vmax + 1)
        data[0, 1] = min(vmax, int(round(meta['exposure_ms'])))
        data[0, 2] = meta.get('trigger', 0) % (vmax + 1)
        incomplete = False
        if self.fault['incomplete_frames'] > 0:
            self.fault['incomplete_frames'] -= 1
            incomplete = True
        meta = dict(meta, frame_id=self.frame_id, t=time.time())
        return FakeImage(data, self.frame_id, incomplete, meta)

    def _sensor_loop(self):
        while True:
            with self.lock:
                if self._stop or not self.streaming:
                    return
                now = time.time()
                ready = []
                if self.n_trigger_mode.symbolic == 'Off':        # continuous: frame period = max(exposure, 1/fps)
                    period = max(self._exposure_s() + READOUT_S, 1.0 / 30.0)
                    if self.paced and self.nodemap.GetNode('AcquisitionFrameRateEnable').value:
                        period = max(1.0 / self.nodemap.GetNode('AcquisitionFrameRate').value, self._exposure_s())
                    if now - self._last_continuous >= period:
                        self._last_continuous = now
                        ready.append(dict(trigger=0, exposure_ms=self.n_exposure.value / 1000.0, gain=self.n_gain.value, source='continuous'))
                keep = []
                for t_ready, meta in self.pending:
                    (ready.append(meta) if t_ready <= now else keep.append((t_ready, meta)))
                self.pending = keep
                images = [self._make_image(m) for m in ready]
                handler = self.handler
                if handler is None:
                    for im in images:
                        if len(self.buffer) >= BUFFERS:
                            self.dropped_full_buffer += 1
                            self._log('dropped', frame=im.frame_id)
                        else:
                            self.buffer.append(im)
                            self._log('buffered', frame=im.frame_id, exposure_ms=im.meta['exposure_ms'])
                    if images:
                        self.cond.notify_all()
            if handler is not None:
                for im in images:
                    self._log('callback', frame=im.frame_id, exposure_ms=im.meta['exposure_ms'])
                    try:
                        handler.OnImageEvent(im)
                    except Exception as e:     # an exception in a Spinnaker callback is printed, the stream goes on
                        print('fake PySpin: exception in OnImageEvent:', repr(e))
            time.sleep(0.002)

    def GetNextImage(self, timeout_ms=None, *a):
        self._enter()
        try:
            end = time.time() + (timeout_ms / 1000.0 if timeout_ms not in (None, -1) else 1e9)
            with self.cond:
                while True:
                    if not self.streaming:
                        raise SpinnakerException('Stream has been aborted', -1010)
                    if self.buffer:
                        im = self.buffer.pop(0)
                        self._log('read', frame=im.frame_id, exposure_ms=im.meta['exposure_ms'], trigger=im.meta.get('trigger'))
                        return im
                    left = end - time.time()
                    if left <= 0:
                        self._log('timeout', timeout_ms=timeout_ms)
                        raise SpinnakerException('Failed waiting for EventData on NEW_BUFFER_DATA event.', -1011)
                    self.cond.wait(min(left, 0.01))
        finally:
            self._leave()


class CameraList:
    def __init__(self, cams):
        self.cams = list(cams)

    def GetSize(self): return len(self.cams)
    def GetByIndex(self, i): return self.cams[i]

    def GetBySerial(self, sn):
        for c in self.cams:
            if c.serial == str(sn):
                return c
        raise SpinnakerException(f'no camera with serial {sn}', -1015)

    def Clear(self): pass
    def __iter__(self): return iter(self.cams)
    def __len__(self): return len(self.cams)


class System:
    _instance = None
    cameras = [FakeCamera()]

    @classmethod
    def GetInstance(cls):
        if cls._instance is None:
            cls._instance = System()
        return cls._instance

    def GetCameras(self): return CameraList(System.cameras)
    def ReleaseInstance(self): pass
    def GetLibraryVersion(self): return type('V', (), dict(major=3, minor=2, type=0, build=62))()


def reset(camera=None):
    """Fresh fake camera (call between tests)."""
    System.cameras = [camera or FakeCamera()]
    return System.cameras[0]
