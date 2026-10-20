"""Option "Empty frame in between" and the checks before an experiment (control/acq_summary.check_exposure_lag).
Run:  python -m pytest tests/test_exposure_lag_option.py"""
import os
import sys
from types import SimpleNamespace as NS

SW = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SW)
from control.acq_summary import check_exposure_lag  # noqa: E402

CONF = [NS(name='488', exposure_time=20), NS(name='561', exposure_time=50), NS(name='BF', exposure_time=20)]
SW_MODE, HW_MODE = 'Software Trigger', 'Hardware Trigger'


def cam(on):
    return NS(exposure_lag_fix=on)


def test_different_exposures_without_tick_block():
    kind, text = check_exposure_lag(cam(False), SW_MODE, CONF, ['488', '561'])
    assert kind == 'block' and 'NOT ticked' in text and '561 50 ms' in text


def test_different_exposures_with_tick_ok():
    assert check_exposure_lag(cam(True), SW_MODE, CONF, ['488', '561'])[0] is None


def test_same_exposures_with_tick_warn():
    kind, text = check_exposure_lag(cam(True), SW_MODE, CONF, ['488', 'BF'])
    assert kind == 'warn' and '20 ms' in text


def test_same_exposures_without_tick_ok():
    assert check_exposure_lag(cam(False), SW_MODE, CONF, ['488', 'BF'])[0] is None


def test_one_channel_hardware_trigger_and_other_cameras_are_ignored():
    assert check_exposure_lag(cam(True), SW_MODE, CONF, ['488'])[0] is None
    assert check_exposure_lag(cam(False), HW_MODE, CONF, ['488', '561'])[0] is None
    assert check_exposure_lag(NS(), SW_MODE, CONF, ['488', '561'])[0] is None
    assert check_exposure_lag(None, SW_MODE, CONF, ['488', '561'])[0] is None
