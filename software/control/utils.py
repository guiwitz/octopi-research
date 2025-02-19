import inspect
import os
import pathlib
import sys
from typing import Optional

import cv2
import git
from numpy import std, square, mean
import numpy as np
from scipy.ndimage import label
from scipy import signal
import os
from typing import Optional, Tuple
from enum import Enum, auto
import squid.logging

_log = squid.logging.get_logger("control.utils")


def crop_image(image, crop_width, crop_height):
    image_height = image.shape[0]
    image_width = image.shape[1]
    roi_left = int(max(image_width / 2 - crop_width / 2, 0))
    roi_right = int(min(image_width / 2 + crop_width / 2, image_width))
    roi_top = int(max(image_height / 2 - crop_height / 2, 0))
    roi_bottom = int(min(image_height / 2 + crop_height / 2, image_height))
    image_cropped = image[roi_top:roi_bottom, roi_left:roi_right]
    return image_cropped


def calculate_focus_measure(image, method="LAPE"):
    if len(image.shape) == 3:
        image = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)  # optional
    if method == "LAPE":
        if image.dtype == np.uint16:
            lap = cv2.Laplacian(image, cv2.CV_32F)
        else:
            lap = cv2.Laplacian(image, cv2.CV_16S)
        focus_measure = mean(square(lap))
    elif method == "GLVA":
        focus_measure = np.std(image, axis=None)  # GLVA
    else:
        focus_measure = np.std(image, axis=None)  # GLVA
    return focus_measure


def unsigned_to_signed(unsigned_array, N):
    signed = 0
    for i in range(N):
        signed = signed + int(unsigned_array[i]) * (256 ** (N - 1 - i))
    signed = signed - (256**N) / 2
    return signed


def rotate_and_flip_image(image, rotate_image_angle, flip_image):
    ret_image = image.copy()
    if rotate_image_angle != 0:
        """
        # ROTATE_90_CLOCKWISE
        # ROTATE_90_COUNTERCLOCKWISE
        """
        if rotate_image_angle == 90:
            ret_image = cv2.rotate(ret_image, cv2.ROTATE_90_CLOCKWISE)
        elif rotate_image_angle == -90:
            ret_image = cv2.rotate(ret_image, cv2.ROTATE_90_COUNTERCLOCKWISE)
        elif rotate_image_angle == 180:
            ret_image = cv2.rotate(ret_image, cv2.ROTATE_180)

    if flip_image is not None:
        """
        flipcode = 0: flip vertically
        flipcode > 0: flip horizontally
        flipcode < 0: flip vertically and horizontally
        """
        if flip_image == "Vertical":
            ret_image = cv2.flip(ret_image, 0)
        elif flip_image == "Horizontal":
            ret_image = cv2.flip(ret_image, 1)
        elif flip_image == "Both":
            ret_image = cv2.flip(ret_image, -1)

    return ret_image


def generate_dpc(im_left, im_right):
    # Normalize the images
    im_left = im_left.astype(float) / 255
    im_right = im_right.astype(float) / 255
    # differential phase contrast calculation
    im_dpc = 0.5 + np.divide(im_left - im_right, im_left + im_right)
    # take care of errors
    im_dpc[im_dpc < 0] = 0
    im_dpc[im_dpc > 1] = 1
    im_dpc[np.isnan(im_dpc)] = 0

    im_dpc = (im_dpc * 255).astype(np.uint8)

    return im_dpc


def colorize_mask(mask):
    # Label the detected objects
    labeled_mask, ___ = label(mask)
    # Color them
    colored_mask = np.array((labeled_mask * 83) % 255, dtype=np.uint8)
    colored_mask = cv2.applyColorMap(colored_mask, cv2.COLORMAP_HSV)
    # make sure background is black
    colored_mask[labeled_mask == 0] = 0
    return colored_mask


def colorize_mask_get_counts(mask):
    # Label the detected objects
    labeled_mask, no_cells = label(mask)
    # Color them
    colored_mask = np.array((labeled_mask * 83) % 255, dtype=np.uint8)
    colored_mask = cv2.applyColorMap(colored_mask, cv2.COLORMAP_HSV)
    # make sure background is black
    colored_mask[labeled_mask == 0] = 0
    return colored_mask, no_cells


def overlay_mask_dpc(color_mask, im_dpc):
    # Overlay the colored mask and DPC image
    # make DPC 3-channel
    im_dpc = np.stack([im_dpc] * 3, axis=2)
    return (0.75 * im_dpc + 0.25 * color_mask).astype(np.uint8)


def centerCrop(image, crop_sz):
    center = image.shape
    x = int(center[1] / 2 - crop_sz / 2)
    y = int(center[0] / 2 - crop_sz / 2)
    cropped = image[y : y + crop_sz, x : x + crop_sz]

    return cropped


def interpolate_plane(triple1, triple2, triple3, point):
    """
    Given 3 triples triple1-3 of coordinates (x,y,z)
    and a pair of coordinates (x,y), linearly interpolates
    the z-value at (x,y).
    """
    # Unpack points
    x1, y1, z1 = triple1
    x2, y2, z2 = triple2
    x3, y3, z3 = triple3

    x, y = point
    # Calculate barycentric coordinates
    detT = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
    if detT == 0:
        raise ValueError("Your 3 x-y coordinates are linear")
    alpha = ((y2 - y3) * (x - x3) + (x3 - x2) * (y - y3)) / detT
    beta = ((y3 - y1) * (x - x3) + (x1 - x3) * (y - y3)) / detT
    gamma = 1 - alpha - beta

    # Interpolate z-coordinate
    z = alpha * z1 + beta * z2 + gamma * z3

    return z


def create_done_file(path):
    with open(os.path.join(path, ".done"), "w") as file:
        pass  # This creates an empty file


def ensure_directory_exists(raw_string_path: str):
    path: pathlib.Path = pathlib.Path(raw_string_path)
    _log.debug(f"Making sure directory '{path}' exists.")
    path.mkdir(parents=True, exist_ok=True)


class SpotDetectionMode(Enum):
    """Specifies which spot to detect when multiple spots are present.

    SINGLE: Expect and detect single spot
    DUAL_RIGHT: In dual-spot case, use rightmost spot
    DUAL_LEFT: In dual-spot case, use leftmost spot
    MULTI_RIGHT: In multi-spot case, use rightmost spot
    MULTI_SECOND_RIGHT: In multi-spot case, use spot immediately left of rightmost spot
    """

    SINGLE = auto()
    DUAL_RIGHT = auto()
    DUAL_LEFT = auto()
    MULTI_RIGHT = auto()
    MULTI_SECOND_RIGHT = auto()


def find_spot_location(
    image: np.ndarray,
    mode: SpotDetectionMode = SpotDetectionMode.SINGLE,
    params: Optional[dict] = None,
    debug_plot: bool = False,
) -> Optional[Tuple[float, float]]:
    """Find the location of a spot in an image.

    Args:
        image: Input grayscale image as numpy array
        mode: Which spot to detect when multiple spots are present
        params: Dictionary of parameters for spot detection. If None, default parameters will be used.
            Supported parameters:
            - y_window (int): Half-height of y-axis crop (default: 96)
            - x_window (int): Half-width of centroid window (default: 20)
            - peak_width (int): Minimum width of peaks (default: 10)
            - peak_distance (int): Minimum distance between peaks (default: 10)
            - peak_prominence (float): Minimum peak prominence (default: 100)
            - intensity_threshold (float): Threshold for intensity filtering (default: 0.1)
            - spot_spacing (int): Expected spacing between spots for multi-spot modes (default: 100)

    Returns:
        Optional[Tuple[float, float]]: (x,y) coordinates of selected spot, or None if detection fails

    Raises:
        ValueError: If image is invalid or mode is incompatible with detected spots
    """
    # Input validation
    if image is None or not isinstance(image, np.ndarray):
        raise ValueError("Invalid input image")

    # Default parameters
    default_params = {
        "y_window": 96,  # Half-height of y-axis crop
        "x_window": 20,  # Half-width of centroid window
        "min_peak_width": 10,  # Minimum width of peaks
        "min_peak_distance": 10,  # Minimum distance between peaks
        "min_peak_prominence": 0.25,  # Minimum peak prominence
        "intensity_threshold": 0.1,  # Threshold for intensity filtering
        "spot_spacing": 100,  # Expected spacing between spots
    }

    if params is not None:
        default_params.update(params)
    p = default_params

    try:
        # Get the y position of the spots
        y_intensity_profile = np.sum(image, axis=1)
        if np.all(y_intensity_profile == 0):
            raise ValueError("No spots detected in image")

        peak_y = np.argmax(y_intensity_profile)

        # Validate peak_y location
        if peak_y < p["y_window"] or peak_y > image.shape[0] - p["y_window"]:
            raise ValueError("Spot too close to image edge")

        # Crop along the y axis
        cropped_image = image[peak_y - p["y_window"] : peak_y + p["y_window"], :]

        # Get signal along x
        x_intensity_profile = np.sum(cropped_image, axis=0)

        # Normalize intensity profile
        x_intensity_profile = x_intensity_profile - np.min(x_intensity_profile)
        x_intensity_profile = x_intensity_profile / np.max(x_intensity_profile)

        # Find all peaks
        peaks = signal.find_peaks(
            x_intensity_profile,
            width=p["min_peak_width"],
            distance=p["min_peak_distance"],
            prominence=p["min_peak_prominence"],
        )
        peak_locations = peaks[0]
        peak_properties = peaks[1]

        if len(peak_locations) == 0:
            raise ValueError("No peaks detected")

        # Handle different spot detection modes
        if mode == SpotDetectionMode.SINGLE:
            if len(peak_locations) > 1:
                raise ValueError(f"Found {len(peak_locations)} peaks but expected single peak")
            peak_x = peak_locations[0]
        elif mode == SpotDetectionMode.DUAL_RIGHT:
            peak_x = peak_locations[-1]
        elif mode == SpotDetectionMode.DUAL_LEFT:
            peak_x = peak_locations[0]
        elif mode == SpotDetectionMode.MULTI_RIGHT:
            peak_x = peak_locations[-1]
        elif mode == SpotDetectionMode.MULTI_SECOND_RIGHT:
            raise NotImplementedError("MULTI_SECOND_RIGHT is not supported")
            # if len(peak_locations) < 2:
            #     raise ValueError("Not enough peaks for MULTI_SECOND_RIGHT mode")
            # peak_x = peak_locations[-2]
            # (peak_x, _) = _calculate_spot_centroid(cropped_image, peak_x, peak_y, p)
            # peak_x = peak_x - p["spot_spacing"]
        else:
            raise ValueError(f"Unknown spot detection mode: {mode}")

        if debug_plot:
            import matplotlib.pyplot as plt

            fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(10, 8))

            # Plot original image
            ax1.imshow(image, cmap="gray")
            ax1.axhline(y=peak_y, color="r", linestyle="--", label="Peak Y")
            ax1.axhline(y=peak_y - p["y_window"], color="g", linestyle="--", label="Crop Window")
            ax1.axhline(y=peak_y + p["y_window"], color="g", linestyle="--")
            ax1.legend()
            ax1.set_title("Original Image with Y-crop Lines")

            # Plot Y intensity profile
            ax2.plot(y_intensity_profile)
            ax2.axvline(x=peak_y, color="r", linestyle="--", label="Peak Y")
            ax2.axvline(x=peak_y - p["y_window"], color="g", linestyle="--", label="Crop Window")
            ax2.axvline(x=peak_y + p["y_window"], color="g", linestyle="--")
            ax2.legend()
            ax2.set_title("Y Intensity Profile")

            # Plot X intensity profile and detected peaks
            ax3.plot(x_intensity_profile, label="Intensity Profile")
            ax3.plot(peak_locations, x_intensity_profile[peak_locations], "x", color="r", label="All Peaks")

            # Plot prominence for all peaks
            for peak_idx, prominence in zip(peak_locations, peak_properties["prominences"]):
                ax3.vlines(
                    x=peak_idx,
                    ymin=x_intensity_profile[peak_idx] - prominence,
                    ymax=x_intensity_profile[peak_idx],
                    color="g",
                )

            # Highlight selected peak
            ax3.plot(peak_x, x_intensity_profile[peak_x], "o", color="yellow", markersize=10, label="Selected Peak")
            ax3.axvline(x=peak_x, color="yellow", linestyle="--", alpha=0.5)

            ax3.legend()
            ax3.set_title(f"X Intensity Profile (Mode: {mode.name})")

            plt.tight_layout()
            plt.show()

        # Calculate centroid in window around selected peak
        return _calculate_spot_centroid(cropped_image, peak_x, peak_y, p)

    except (ValueError, NotImplementedError) as e:
        raise e
    except Exception:
        # TODO: this should not be a blank Exception catch, we should jsut return None above if we have a valid "no spots"
        # case, and let exceptions raise otherwise.
        _log.exception(f"Error in spot detection")
        return None


def _calculate_spot_centroid(cropped_image: np.ndarray, peak_x: int, peak_y: int, params: dict) -> Tuple[float, float]:
    """Calculate precise centroid location in window around peak."""
    h, w = cropped_image.shape
    x, y = np.meshgrid(range(w), range(h))

    # Crop region around the peak
    intensity_window = cropped_image[:, peak_x - params["x_window"] : peak_x + params["x_window"]]
    x_coords = x[:, peak_x - params["x_window"] : peak_x + params["x_window"]]
    y_coords = y[:, peak_x - params["x_window"] : peak_x + params["x_window"]]

    # Process intensity values
    intensity_window = intensity_window.astype(float)
    intensity_window = intensity_window - np.amin(intensity_window)
    if np.amax(intensity_window) > 0:  # Avoid division by zero
        intensity_window[intensity_window / np.amax(intensity_window) < params["intensity_threshold"]] = 0

    # Calculate centroid
    sum_intensity = np.sum(intensity_window)
    if sum_intensity == 0:
        raise ValueError("No significant intensity in centroid window")

    centroid_x = np.sum(x_coords * intensity_window) / sum_intensity
    centroid_y = np.sum(y_coords * intensity_window) / sum_intensity

    # Convert back to original image coordinates
    centroid_y = peak_y - params["y_window"] + centroid_y

    return (centroid_x, centroid_y)


def get_squid_repo_state_description() -> Optional[str]:
    # From here: https://stackoverflow.com/a/22881871
    def get_script_dir(follow_symlinks=True):
        if getattr(sys, "frozen", False):  # py2exe, PyInstaller, cx_Freeze
            path = os.path.abspath(sys.executable)
        else:
            path = inspect.getabsfile(get_script_dir)
        if follow_symlinks:
            path = os.path.realpath(path)
        return os.path.dirname(path)

    try:
        repo = git.Repo(get_script_dir(), search_parent_directories=True)
        return f"{repo.head.object.hexsha} (dirty={repo.is_dirty()})"
    except git.GitError as e:
        _log.warning(f"Failed to get script git repo info: {e}")
        return None
