import argparse
import cv2
import time
import numpy as np
try:
    import control.gxipy as gx
except:
    print('gxipy import error')

from control._def import *

class Camera(object):

    @staticmethod
    def _event_callback(nEvent, camera):
        if nEvent == toupcam.TOUPCAM_EVENT_IMAGE:
            camera._on_frame_callback()

    def _on_frame_callback(self):
        try:
            self.camera.PullImageV2(self.buf, 24, None)
            self.frame_ID_software += 1
            self.frame_ID += 1
            print('pull image ok, total = {}'.format(self.total))
        except toupcam.HRESULTException as ex:
            print('pull image failed, hr=0x{:x}'.format(ex.hr))

        raw_array = numpy.array(bytearray(self.buf))
        self.current_frame = raw_array.reshape(self.height,self.width)

        # if raw_image is None:
        #     print("Getting image failed.")
        #     return
        # if raw_image.get_status() != 0:
        #     print("Got an incomplete frame")
        #     return
        # if self.image_locked:
        #     print('last image is still being processed, a frame is dropped')
        #     return
        # if self.is_color:
        #     rgb_image = raw_image.convert("RGB")
        #     numpy_image = rgb_image.get_numpy_array()
        #     if self.pixel_format == 'BAYER_RG12':
        #         numpy_image = numpy_image << 4
        # else:
        #     numpy_image = raw_image.get_numpy_array()
        #     if self.pixel_format == 'MONO12':
        #         numpy_image = numpy_image << 4
        # if numpy_image is None:
        #     return
        # self.current_frame = numpy_image
        # self.frame_ID_software = self.frame_ID_software + 1
        # self.frame_ID = raw_image.get_frame_id()
        # if self.trigger_mode == TriggerMode.HARDWARE:
        #     if self.frame_ID_offset_hardware_trigger == None:
        #         self.frame_ID_offset_hardware_trigger = self.frame_ID
        #     self.frame_ID = self.frame_ID - self.frame_ID_offset_hardware_trigger
        # self.timestamp = time.time()

        self.new_image_callback_external(self)

    def __init__(self,sn=None,is_global_shutter=False,rotate_image_angle=None,flip_image=None):

        # many to be purged
        self.sn = sn
        self.is_global_shutter = is_global_shutter
        self.device_manager = gx.DeviceManager()
        self.device_info_list = None
        self.device_index = 0
        self.camera = None
        self.is_color = None
        self.gamma_lut = None
        self.contrast_lut = None
        self.color_correction_param = None

        self.rotate_image_angle = rotate_image_angle
        self.flip_image = flip_image

        self.exposure_time = 1 # unit: ms
        self.analog_gain = 0
        self.frame_ID = -1
        self.frame_ID_software = -1
        self.frame_ID_offset_hardware_trigger = 0
        self.timestamp = 0

        self.image_locked = False
        self.current_frame = None

        self.callback_is_enabled = False
        self.callback_was_enabled_before_autofocus = False
        self.callback_was_enabled_before_multipoint = False
        self.is_streaming = False

        self.GAIN_MAX = 24
        self.GAIN_MIN = 0
        self.GAIN_STEP = 1
        self.EXPOSURE_TIME_MS_MIN = 0.01
        self.EXPOSURE_TIME_MS_MAX = 4000

        self.ROI_offset_x = CAMERA.ROI_OFFSET_X_DEFAULT
        self.ROI_offset_y = CAMERA.ROI_OFFSET_X_DEFAULT
        self.ROI_width = CAMERA.ROI_WIDTH_DEFAULT
        self.ROI_height = CAMERA.ROI_HEIGHT_DEFAULT

        self.trigger_mode = None
        self.pixel_size_byte = 1

        # below are values for IMX226 (MER2-1220-32U3M) - to make configurable 
        self.row_period_us = 10
        self.row_numbers = 3036
        self.exposure_delay_us_8bit = 650
        self.exposure_delay_us = self.exposure_delay_us_8bit*self.pixel_size_byte
        self.strobe_delay_us = self.exposure_delay_us + self.row_period_us*self.pixel_size_byte*(self.row_numbers-1)

        self.pixel_format = None # use the default pixel format

        # toupcam
        self.devices = toupcam.Toupcam.EnumV2()
        
    def open(self,index=0):
        if len(self.devices) > 0:
            print('{}: flag = {:#x}, preview = {}, still = {}'.format(self.devices[0].displayname, self.devices[0].model.flag, self.devices[0].model.preview, self.devices[0].model.still))
            for r in self.devices[index].model.res:
                print('\t = [{} x {}]'.format(r.width, r.height))
            self.camera = toupcam.Toupcam.Open(self.devices[index].id)

            # RGB format: The output of every pixel contains 3 componants which stand for R/G/B value respectively. This output is a processed output from the internal color processing engine.
            # RAW format: In this format, the output is the raw data directly output from the sensor. The RAW format is for the users that want to skip the internal color processing and obtain the raw data for user-specific purpose. With the raw format output enabled, the functions that are related to the internal color processing will not work, such as Toupcam_put_Hue or Toupcam_AwbOnce function and so on
            self.camera.put_Option(self.camera.TOUPCAM_OPTION_RAW,1) # RAW mode
            self.camera.put_Option(self.camera.TOUPCAM_OPTION_BITDEPTH,1) # max bit depth
            
            if self.camera:
                width, height = self.camera.get_Size()
                self.width = width
                self.height = height
                # bufsize = ((width * 24 + 31) // 32 * 4) * height # RGB format
                bufsize = width * height * 2 # 16 bit
                print('image size: {} x {}, bufsize = {}'.format(width, height, bufsize))
                self.buf = bytes(bufsize)
                if self.buf:
                    try:
                        self.camera.StartPullModeWithCallback(self._event_callback, self)
                    except toupcam.HRESULTException as ex:
                        print('failed to start camera, hr=0x{:x}'.format(ex.hr))
                        exit()
            else:
                print('failed to open camera')
                exit()
        else:
            print('no camera found')

        self.is_color = False
        if self.is_color:
            pass

    def set_callback(self,function):
        self.new_image_callback_external = function

    def enable_callback(self):
        pass
        self.callback_is_enabled = True

    def disable_callback(self):
        pass
        self.callback_is_enabled = False

    def open_by_sn(self,sn):
        pass

    def close(self):
        self.camera.Close()
        self.camera = None
        self.buf = None
        self.last_raw_image = None
        self.last_converted_image = None
        self.last_numpy_image = None

    def set_exposure_time(self,exposure_time):
        pass
        # use_strobe = (self.trigger_mode == TriggerMode.HARDWARE) # true if using hardware trigger
        # if use_strobe == False or self.is_global_shutter:
        #     self.exposure_time = exposure_time
        #     self.camera.ExposureTime.set(exposure_time * 1000)
        # else:
        #     # set the camera exposure time such that the active exposure time (illumination on time) is the desired value
        #     self.exposure_time = exposure_time
        #     # add an additional 500 us so that the illumination can fully turn off before rows start to end exposure
        #     camera_exposure_time = self.exposure_delay_us + self.exposure_time*1000 + self.row_period_us*self.pixel_size_byte*(self.row_numbers-1) + 500 # add an additional 500 us so that the illumination can fully turn off before rows start to end exposure
        #     self.camera.ExposureTime.set(camera_exposure_time)

    def update_camera_exposure_time(self):
        pass
        # use_strobe = (self.trigger_mode == TriggerMode.HARDWARE) # true if using hardware trigger
        # if use_strobe == False or self.is_global_shutter:
        #     self.camera.ExposureTime.set(self.exposure_time * 1000)
        # else:
        #     camera_exposure_time = self.exposure_delay_us + self.exposure_time*1000 + self.row_period_us*self.pixel_size_byte*(self.row_numbers-1) + 500 # add an additional 500 us so that the illumination can fully turn off before rows start to end exposure
        #     self.camera.ExposureTime.set(camera_exposure_time)

    def set_analog_gain(self,analog_gain):
        self.analog_gain = analog_gain
        # self.camera.Gain.set(analog_gain)

    def get_awb_ratios(self):
        pass

    def set_wb_ratios(self, wb_r=None, wb_g=None, wb_b=None):
        pass

    def set_reverse_x(self,value):
        pass

    def set_reverse_y(self,value):
        pass

    def start_streaming(self):
        # self.camera.stream_on()
        self.is_streaming = True

    def stop_streaming(self):
        # self.camera.stream_off()
        self.is_streaming = False

    def set_pixel_format(self,pixel_format):
        # if self.is_streaming == True:
        #     was_streaming = True
        #     self.stop_streaming()
        # else:
        #     was_streaming = False

        # if self.camera.PixelFormat.is_implemented() and self.camera.PixelFormat.is_writable():
        #     if pixel_format == 'MONO8':
        #         self.camera.PixelFormat.set(gx.GxPixelFormatEntry.MONO8)
        #         self.pixel_size_byte = 1
        #     if pixel_format == 'MONO12':
        #         self.camera.PixelFormat.set(gx.GxPixelFormatEntry.MONO12)
        #         self.pixel_size_byte = 2
        #     if pixel_format == 'MONO14':
        #         self.camera.PixelFormat.set(gx.GxPixelFormatEntry.MONO14)
        #         self.pixel_size_byte = 2
        #     if pixel_format == 'MONO16':
        #         self.camera.PixelFormat.set(gx.GxPixelFormatEntry.MONO16)
        #         self.pixel_size_byte = 2
        #     if pixel_format == 'BAYER_RG8':
        #         self.camera.PixelFormat.set(gx.GxPixelFormatEntry.BAYER_RG8)
        #         self.pixel_size_byte = 1
        #     if pixel_format == 'BAYER_RG12':
        #         self.camera.PixelFormat.set(gx.GxPixelFormatEntry.BAYER_RG12)
        #         self.pixel_size_byte = 2
        #     self.pixel_format = pixel_format
        # else:
        #     print("pixel format is not implemented or not writable")

        # if was_streaming:
        #    self.start_streaming()

        # # update the exposure delay and strobe delay
        # self.exposure_delay_us = self.exposure_delay_us_8bit*self.pixel_size_byte
        # self.strobe_delay_us = self.exposure_delay_us + self.row_period_us*self.pixel_size_byte*(self.row_numbers-1)
        pass

    def set_continuous_acquisition(self):
        # self.camera.TriggerMode.set(gx.GxSwitchEntry.OFF)
        # self.trigger_mode = TriggerMode.CONTINUOUS
        # self.update_camera_exposure_time()
        pass

    def set_software_triggered_acquisition(self):
        # self.camera.TriggerMode.set(gx.GxSwitchEntry.ON)
        # self.camera.TriggerSource.set(gx.GxTriggerSourceEntry.SOFTWARE)
        # self.trigger_mode = TriggerMode.SOFTWARE
        # self.update_camera_exposure_time()
        pass

    def set_hardware_triggered_acquisition(self):
        # self.camera.TriggerMode.set(gx.GxSwitchEntry.ON)
        # self.camera.TriggerSource.set(gx.GxTriggerSourceEntry.LINE2)
        # # self.camera.TriggerSource.set(gx.GxTriggerActivationEntry.RISING_EDGE)
        # self.frame_ID_offset_hardware_trigger = None
        # self.trigger_mode = TriggerMode.HARDWARE
        # self.update_camera_exposure_time()
        pass

    def send_trigger(self):
        # if self.is_streaming:
        #     self.camera.TriggerSoftware.send_command()
        # else:
        # 	print('trigger not sent - camera is not streaming')
        pass

    def read_frame(self):
        # raw_image = self.camera.data_stream[self.device_index].get_image()
        # if self.is_color:
        #     rgb_image = raw_image.convert("RGB")
        #     numpy_image = rgb_image.get_numpy_array()
        #     if self.pixel_format == 'BAYER_RG12':
        #         numpy_image = numpy_image << 4
        # else:
        #     numpy_image = raw_image.get_numpy_array()
        #     if self.pixel_format == 'MONO12':
        #         numpy_image = numpy_image << 4
        # # self.current_frame = numpy_image
        # return numpy_image
        pass
    
    def set_ROI(self,offset_x=None,offset_y=None,width=None,height=None):
        # if offset_x is not None:
        #     self.ROI_offset_x = offset_x
        #     # stop streaming if streaming is on
        #     if self.is_streaming == True:
        #         was_streaming = True
        #         self.stop_streaming()
        #     else:
        #         was_streaming = False
        #     # update the camera setting
        #     if self.camera.OffsetX.is_implemented() and self.camera.OffsetX.is_writable():
        #         self.camera.OffsetX.set(self.ROI_offset_x)
        #     else:
        #         print("OffsetX is not implemented or not writable")
        #     # restart streaming if it was previously on
        #     if was_streaming == True:
        #         self.start_streaming()

        # if offset_y is not None:
        #     self.ROI_offset_y = offset_y
        #         # stop streaming if streaming is on
        #     if self.is_streaming == True:
        #         was_streaming = True
        #         self.stop_streaming()
        #     else:
        #         was_streaming = False
        #     # update the camera setting
        #     if self.camera.OffsetY.is_implemented() and self.camera.OffsetY.is_writable():
        #         self.camera.OffsetY.set(self.ROI_offset_y)
        #     else:
        #         print("OffsetX is not implemented or not writable")
        #     # restart streaming if it was previously on
        #     if was_streaming == True:
        #         self.start_streaming()

        # if width is not None:
        #     self.ROI_width = width
        #     # stop streaming if streaming is on
        #     if self.is_streaming == True:
        #         was_streaming = True
        #         self.stop_streaming()
        #     else:
        #         was_streaming = False
        #     # update the camera setting
        #     if self.camera.Width.is_implemented() and self.camera.Width.is_writable():
        #         self.camera.Width.set(self.ROI_width)
        #     else:
        #         print("OffsetX is not implemented or not writable")
        #     # restart streaming if it was previously on
        #     if was_streaming == True:
        #         self.start_streaming()

        # if height is not None:
        #     self.ROI_height = height
        #     # stop streaming if streaming is on
        #     if self.is_streaming == True:
        #         was_streaming = True
        #         self.stop_streaming()
        #     else:
        #         was_streaming = False
        #     # update the camera setting
        #     if self.camera.Height.is_implemented() and self.camera.Height.is_writable():
        #         self.camera.Height.set(self.ROI_height)
        #     else:
        #         print("Height is not implemented or not writable")
        #     # restart streaming if it was previously on
        #     if was_streaming == True:
        #         self.start_streaming()
        pass

    def reset_camera_acquisition_counter(self):
        # if self.camera.CounterEventSource.is_implemented() and self.camera.CounterEventSource.is_writable():
        #     self.camera.CounterEventSource.set(gx.GxCounterEventSourceEntry.LINE2)
        # else:
        #     print("CounterEventSource is not implemented or not writable")

        # if self.camera.CounterReset.is_implemented():
        #     self.camera.CounterReset.send_command()
        # else:
        #     print("CounterReset is not implemented")
        pass

    def set_line3_to_strobe(self):
        # # self.camera.StrobeSwitch.set(gx.GxSwitchEntry.ON)
        # self.camera.LineSelector.set(gx.GxLineSelectorEntry.LINE3)
        # self.camera.LineMode.set(gx.GxLineModeEntry.OUTPUT)
        # self.camera.LineSource.set(gx.GxLineSourceEntry.STROBE)
        pass

    def set_line3_to_exposure_active(self):
        # # self.camera.StrobeSwitch.set(gx.GxSwitchEntry.ON)
        # self.camera.LineSelector.set(gx.GxLineSelectorEntry.LINE3)
        # self.camera.LineMode.set(gx.GxLineModeEntry.OUTPUT)
        # self.camera.LineSource.set(gx.GxLineSourceEntry.EXPOSURE_ACTIVE)
        pass

class Camera_Simulation(object):
    
    def __init__(self,sn=None,is_global_shutter=False,rotate_image_angle=None,flip_image=None):
        # many to be purged
        self.sn = sn
        self.is_global_shutter = is_global_shutter
        self.device_info_list = None
        self.device_index = 0
        self.camera = None
        self.is_color = None
        self.gamma_lut = None
        self.contrast_lut = None
        self.color_correction_param = None

        self.rotate_image_angle = rotate_image_angle
        self.flip_image = flip_image

        self.exposure_time = 0
        self.analog_gain = 0
        self.frame_ID = 0
        self.frame_ID_software = -1
        self.frame_ID_offset_hardware_trigger = 0
        self.timestamp = 0

        self.image_locked = False
        self.current_frame = None

        self.callback_is_enabled = False
        self.callback_was_enabled_before_autofocus = False
        self.callback_was_enabled_before_multipoint = False

        self.GAIN_MAX = 24
        self.GAIN_MIN = 0
        self.GAIN_STEP = 1
        self.EXPOSURE_TIME_MS_MIN = 0.01
        self.EXPOSURE_TIME_MS_MAX = 4000

        self.trigger_mode = None
        self.pixel_size_byte = 1

        # below are values for IMX226 (MER2-1220-32U3M) - to make configurable 
        self.row_period_us = 10
        self.row_numbers = 3036
        self.exposure_delay_us_8bit = 650
        self.exposure_delay_us = self.exposure_delay_us_8bit*self.pixel_size_byte
        self.strobe_delay_us = self.exposure_delay_us + self.row_period_us*self.pixel_size_byte*(self.row_numbers-1)

        self.pixel_format = 'MONO8'

    def open(self,index=0):
        pass

    def set_callback(self,function):
        self.new_image_callback_external = function

    def enable_callback(self):
        self.callback_is_enabled = True

    def disable_callback(self):
        self.callback_is_enabled = False

    def open_by_sn(self,sn):
        pass

    def close(self):
        pass

    def set_exposure_time(self,exposure_time):
        pass

    def update_camera_exposure_time(self):
        pass

    def set_analog_gain(self,analog_gain):
        pass

    def get_awb_ratios(self):
        pass

    def set_wb_ratios(self, wb_r=None, wb_g=None, wb_b=None):
        pass

    def start_streaming(self):
        self.frame_ID_software = 0

    def stop_streaming(self):
        pass

    def set_pixel_format(self,pixel_format):
        self.pixel_format = pixel_format
        print(pixel_format)
        self.frame_ID = 0

    def set_continuous_acquisition(self):
        pass

    def set_software_triggered_acquisition(self):
        pass

    def set_hardware_triggered_acquisition(self):
        pass

    def send_trigger(self):
        self.frame_ID = self.frame_ID + 1
        self.timestamp = time.time()
        if self.frame_ID == 1:
            if self.pixel_format == 'MONO8':
                self.current_frame = np.random.randint(255,size=(2000,2000),dtype=np.uint8)
                self.current_frame[901:1100,901:1100] = 200
            elif self.pixel_format == 'MONO12':
                self.current_frame = np.random.randint(4095,size=(2000,2000),dtype=np.uint16)
                self.current_frame[901:1100,901:1100] = 200*16
                self.current_frame = self.current_frame << 4
            elif self.pixel_format == 'MONO16':
                self.current_frame = np.random.randint(65535,size=(2000,2000),dtype=np.uint16)
                self.current_frame[901:1100,901:1100] = 200*256
        else:
            self.current_frame = np.roll(self.current_frame,10,axis=0)
            pass 
            # self.current_frame = np.random.randint(255,size=(768,1024),dtype=np.uint8)
        if self.new_image_callback_external is not None and self.callback_is_enabled:
            self.new_image_callback_external(self)

    def read_frame(self):
        return self.current_frame

    def _on_frame_callback(self, user_param, raw_image):
        pass

    def set_ROI(self,offset_x=None,offset_y=None,width=None,height=None):
        pass

    def reset_camera_acquisition_counter(self):
        pass

    def set_line3_to_strobe(self):
        pass

    def set_line3_to_exposure_active(self):
        pass