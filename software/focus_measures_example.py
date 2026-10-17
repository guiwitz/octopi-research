"""Example custom focus measures for the Contrast AF tab ("Load custom .py...").

Every public function (name not starting with '_') that takes ONE argument - the image as a
NumPy array (uint8 or uint16, 2-D or 3-D) - and returns ONE finite number is offered in the
focus measure dropdown. Larger values must mean "more in focus". Copy this file, rename it and
write your own; the file is executed when it is loaded, so only load files you trust.
"""
import cv2
import numpy as np


def _gray(image):
    return cv2.cvtColor(image, cv2.COLOR_RGB2GRAY) if image.ndim == 3 else image


def tenengrad(image):
    """Mean squared Sobel gradient magnitude."""
    img = _gray(image).astype(np.float32)
    gx = cv2.Sobel(img, cv2.CV_32F, 1, 0)
    gy = cv2.Sobel(img, cv2.CV_32F, 0, 1)
    return float(np.mean(gx * gx + gy * gy))


def normalized_variance(image):
    """Variance divided by the mean intensity (less sensitive to illumination changes)."""
    img = _gray(image).astype(np.float32)
    mean = float(np.mean(img))
    return float(np.var(img) / mean) if mean > 0 else 0.0
