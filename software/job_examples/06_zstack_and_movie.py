"""Every 30 min: autofocus, a 2-colour z-stack and a short movie at each position (lossy JPEG2000 for the movie)."""

STACK_CHANNELS = ["488", "561"]
N_TIMEPOINTS = 12
INTERVAL_S = 1800


def run_job(scope):
    """WHAT IT DOES: at every time point and position: autofocus, then an 11-plane z-stack (1 um apart, centred on the focused z) in
    488 and 561 (all channels per plane: order="z_major"), then a 20 s movie in 488 (one image per second), saved as lossy JPEG2000
    (error = 1x noise per channel; see lossy_files.csv for what was achieved) in the multipage TIFF; the stack stays lossless.

    YOU PROVIDE: positions in the Flexible Multipoint tab; channel names matching the channel configuration; for lossy saving the
    Python package imagecodecs (the check tells you). Movies: use one exposure for all channels of a movie."""
    for t in scope.timepoints(N_TIMEPOINTS, interval=INTERVAL_S):
        for pt in scope.points():
            scope.move_to(pt)
            scope.autofocus()
            scope.zstack(STACK_CHANNELS, planes=11, step_um=1.0, order="z_major")
            scope.movie("488", duration_s=20, interval_s=1.0, compression="lossy")           # default: error = 1x noise
