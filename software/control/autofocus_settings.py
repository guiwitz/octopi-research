"""Autofocus focus-measure registry, custom focus-measure files and settings persistence.

This module has no GUI code. The autofocus controller (core.AutoFocusController) holds the
current settings; the Contrast AF tab only displays and edits them.

Focus measure names
-------------------
'LAPE', 'GLVA'                  built-in measures (control.utils.calculate_focus_measure)
'CUSTOM:<path.py>::<function>'  a public function f(image) -> float defined in a user file

A custom function receives the (cropped) camera image as a NumPy array (uint8 or uint16,
2-D or 3-D) and returns a single finite number; larger = more in focus. The code in the file
is executed when the file is loaded, so only load files you trust.
"""

import importlib.util
import inspect
import json
import os
import traceback

import numpy as np

import control.utils as utils

BUILTIN_MEASURES = ('LAPE', 'GLVA')
CUSTOM_PREFIX = 'CUSTOM:'
SETTINGS_VERSION = 1
LAST_SETTINGS_FILE_CACHE = os.path.join('cache', 'autofocus_settings_path.txt')


class FocusMeasureRegistry:
    """Knows the built-in measures and the custom functions loaded from user files."""

    def __init__(self):
        self._funcs = {}   # key -> callable
        self._files = {}   # absolute path -> list of keys loaded from it

    # ---- keys ----------------------------------------------------------------------
    @staticmethod
    def make_key(path, func_name):
        return f'{CUSTOM_PREFIX}{os.path.abspath(path)}::{func_name}'

    @staticmethod
    def is_custom(key):
        return isinstance(key, str) and key.startswith(CUSTOM_PREFIX)

    @staticmethod
    def split_key(key):
        path, func_name = key[len(CUSTOM_PREFIX):].rsplit('::', 1)
        return path, func_name

    def label(self, key):
        """Short text for dropdowns."""
        if not self.is_custom(key):
            return key
        path, func_name = self.split_key(key)
        return f'{func_name}  ({os.path.basename(path)})'

    def keys(self):
        return list(BUILTIN_MEASURES) + list(self._funcs.keys())

    # ---- loading custom files --------------------------------------------------------
    @staticmethod
    def _test_images():
        rng = np.random.default_rng(0)
        img8 = rng.integers(0, 255, size=(96, 96), dtype=np.uint8)
        img8[40:56, 40:56] = 200
        img16 = rng.integers(0, 65535, size=(96, 96), dtype=np.uint16)
        return [img8, img16, np.stack([img8] * 3, axis=-1)]

    def load_file(self, path):
        """Load all public functions of a .py file as custom focus measures.

        Returns (keys, problems): keys of the usable functions and a list of readable
        problem descriptions (functions that failed the test call are skipped).
        Raises ValueError if the file cannot be loaded or contains no usable function.
        """
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            raise ValueError(f'File not found: {path}')
        module_name = f'_af_custom_{abs(hash(path))}'
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as e:
            traceback.print_exc()
            raise ValueError(f'Could not load {os.path.basename(path)}: {e}')

        # forget functions from an earlier load of the same file
        for old_key in self._files.get(path, []):
            self._funcs.pop(old_key, None)

        keys, problems = [], []
        for name, func in inspect.getmembers(module, inspect.isfunction):
            if name.startswith('_') or func.__module__ != module_name:
                continue
            try:
                n_required = sum(1 for p in inspect.signature(func).parameters.values()
                                 if p.default is inspect.Parameter.empty
                                 and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD))
                if n_required != 1:
                    problems.append(f'{name}: must take exactly one required argument (the image)')
                    continue
                for img in self._test_images():
                    value = float(func(img.copy()))
                    if not np.isfinite(value):
                        raise ValueError('returned a non-finite value')
            except Exception as e:
                problems.append(f'{name}: {e}')
                continue
            key = self.make_key(path, name)
            self._funcs[key] = func
            keys.append(key)

        self._files[path] = keys
        if not keys:
            raise ValueError('No usable function found in ' + os.path.basename(path)
                             + (': ' + '; '.join(problems) if problems else ''))
        return keys, problems

    def get(self, key):
        """Return the callable for a custom key, loading its file first if necessary."""
        if key not in self._funcs:
            path, _ = self.split_key(key)
            self.load_file(path)
        if key not in self._funcs:
            raise ValueError(f'Focus measure not available: {key}')
        return self._funcs[key]

    # ---- evaluation ------------------------------------------------------------------
    def calculate(self, image, key):
        """Focus measure of one image. Raises if a custom function fails."""
        if self.is_custom(key):
            return float(self.get(key)(image))
        return utils.calculate_focus_measure(image, key)


REGISTRY = FocusMeasureRegistry()


# ---- settings files ----------------------------------------------------------------------
def save_settings(path, settings):
    """Write a settings dict (see AutoFocusController.get_settings) as JSON."""
    data = dict(settings)
    data['version'] = SETTINGS_VERSION
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)


def load_settings(path):
    """Read and validate a settings file. Raises ValueError with a readable message."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        raise ValueError(f'Could not read {os.path.basename(path)}: {e}')
    if not isinstance(data, dict):
        raise ValueError('Not an autofocus settings file')
    clean = {}
    if 'focus_measure' in data:
        if not isinstance(data['focus_measure'], str):
            raise ValueError('focus_measure must be text')
        clean['focus_measure'] = data['focus_measure']
    if 'af_channel' in data:
        if not isinstance(data['af_channel'], str):
            raise ValueError('af_channel must be text')
        clean['af_channel'] = data['af_channel']
    if 'delta_z_um' in data:
        value = float(data['delta_z_um'])
        if not 0 < value <= 20:
            raise ValueError('delta_z_um must be between 0 and 20')
        clean['delta_z_um'] = value
    if 'n_planes' in data:
        value = int(data['n_planes'])
        if not 3 <= value <= 20:
            raise ValueError('n_planes must be between 3 and 20')
        clean['n_planes'] = value
    if 'af_schedule_mode' in data:
        if data['af_schedule_mode'] not in ('every_timepoint', 'every_n', 'staggered'):
            raise ValueError("af_schedule_mode must be 'every_timepoint', 'every_n' or 'staggered'")
        clean['af_schedule_mode'] = data['af_schedule_mode']
    if 'af_schedule_n' in data:
        value = int(data['af_schedule_n'])
        if not 1 <= value <= 100000:
            raise ValueError('af_schedule_n must be between 1 and 100000')
        clean['af_schedule_n'] = value
    if 'af_all_at_t0' in data:
        if not isinstance(data['af_all_at_t0'], bool):
            raise ValueError('af_all_at_t0 must be true or false')
        clean['af_all_at_t0'] = data['af_all_at_t0']
    if not clean:
        raise ValueError('No autofocus settings found in the file')
    return clean


# ---- short descriptions for the GUI --------------------------------------------------------
def summary_text(settings, registry=REGISTRY):
    """One-line summary of the autofocus conditions, e.g. 'dZ 1.524 um | N 10 | LAPE'."""
    dz = settings.get('delta_z_um')
    n = settings.get('n_planes')
    key = settings.get('focus_measure')
    dz_text = f'{dz:g}' if dz is not None else '?'
    n_text = str(n) if n is not None else '?'
    measure = registry.label(key).split('  (')[0] if key else '?'
    text = f'dZ {dz_text} um | N {n_text} | {measure}'
    schedule = schedule_text(settings)
    return text + (' | ' + schedule if schedule else '')


def schedule_text(settings):
    """Short description of when the autofocus runs; empty if it runs at every time point."""
    mode = settings.get('af_schedule_mode', 'every_timepoint')
    n = settings.get('af_schedule_n', 1)
    if mode == 'every_n' and n > 1:
        return f'AF every {n} tp'
    if mode == 'staggered' and n > 1:
        return f'AF staggered /{n}' + (', all at t=0' if settings.get('af_all_at_t0', True) else '')
    return ''


def tooltip_text(settings, registry=REGISTRY):
    """Longer description of the autofocus conditions for a tooltip."""
    dz = settings.get('delta_z_um')
    n = settings.get('n_planes')
    lines = ['Contrast autofocus conditions (set in the Contrast AF tab):']
    if dz is not None and n is not None:
        lines.append(f'dZ {dz:g} um x N {n} planes = scan range {dz * n:g} um')
    lines.append('Focus measure: ' + (registry.label(settings.get('focus_measure')) if settings.get('focus_measure') else '?'))
    lines.append('Autofocus channel: ' + str(settings.get('af_channel')))
    mode = settings.get('af_schedule_mode', 'every_timepoint')
    n = settings.get('af_schedule_n', 1)
    if mode == 'every_n' and n > 1:
        lines.append(f'Runs at time points t = 0, {n}, {2 * n}, ... (all positions); t = 0 is the first time point')
    elif mode == 'staggered' and n > 1:
        lines.append(f'Staggered: position p runs when (p - t) is divisible by {n} (positions counted from 1, '
                     f't = 0 is the first time point)' + ('; all positions at t = 0' if settings.get('af_all_at_t0', True) else ''))
    else:
        lines.append('Runs at every time point')
    return '\n'.join(lines)


# ---- remember the last used settings file ------------------------------------------------
def remember_settings_file(path):
    try:
        os.makedirs(os.path.dirname(LAST_SETTINGS_FILE_CACHE), exist_ok=True)
        with open(LAST_SETTINGS_FILE_CACHE, 'w', encoding='utf-8') as f:
            f.write(os.path.abspath(path))
    except OSError as e:
        print('Could not remember autofocus settings file:', e)


def last_settings_file():
    """Path of the last used settings file, or None."""
    try:
        with open(LAST_SETTINGS_FILE_CACHE, 'r', encoding='utf-8') as f:
            path = f.read().strip()
        return path if path and os.path.isfile(path) else None
    except OSError:
        return None
