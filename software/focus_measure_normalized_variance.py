"""Custom autofocus focus measure: normalized variance (load it with "Load custom .py..." in the Contrast AF tab).

    normalized variance = variance(I) / mean(I)

The sharpest plane (best focus) has the MAXIMUM normalized variance. Dividing by the mean makes the value
less sensitive to changes in overall brightness between planes (lamp drift, bleaching, different illumination).
Works on uint8 / uint16 images, 2-D or 3-D (RGB is converted to grey). Computed in float64.

In a job:  scope.autofocus(channel="561", focus_measure="focus_measure_normalized_variance.py::normalized_variance")
(the path is relative to the software folder, or absolute).
"""
import cv2
import numpy as np


def normalized_variance(image):
    """Variance / mean of the pixel intensities; larger = more in focus."""
    img = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY) if image.ndim == 3 else image
    img = img.astype(np.float64)
    mean = img.mean()
    if mean <= 0:
        return 0.0
    return float(img.var() / mean)
