"""Two phases: dense time lapse of group A, then one overview of all positions (example job)."""

# ---- settings: edit these ------------------------------------------------------------------
GROUP_TIMELAPSE = "A"                          # group imaged in phase 1 (part of the "group" column of the positions table before the first "-")
TIMELAPSE_CHANNEL = "488"
N_TIMEPOINTS, INTERVAL_S = 12, 300             # phase 1: 12 time points, one every 5 min
OVERVIEW_AT_S = 2 * 3600                       # phase 2 starts this many seconds after the start of the job
OVERVIEW_CHANNELS = ["BF LED matrix full", "488"]


def run_job(scope):
    """WHAT IT DOES: phase 1: every INTERVAL_S seconds, images the positions of group GROUP_TIMELAPSE in TIMELAPSE_CHANNEL
    (N_TIMEPOINTS times) and warns if an image is completely empty. Phase 2: waits until OVERVIEW_AT_S seconds after the
    start of the job, then visits ALL positions, runs the autofocus and images OVERVIEW_CHANNELS.
    Shows scope.wait_until() and that images come back as NumPy arrays (decisions can depend on image content).

    YOU PROVIDE:
      - Positions in the Flexible Multipoint tab; the positions of the time-lapse group need a group cell starting with
        GROUP_TIMELAPSE + "-", e.g. A-0, A-1 (the other positions can have any other group).
      - Settings at the top of this file (group, channels, number/interval of time points, start time of phase 2).
    """
    for t in scope.timepoints(N_TIMEPOINTS, interval=INTERVAL_S):
        for pt in scope.points(GROUP_TIMELAPSE):
            scope.move_to(pt)
            image = scope.capture(TIMELAPSE_CHANNEL)
            if image.max() == 0:
                scope.warn(f"empty image at {pt.id}")
    scope.wait_until(OVERVIEW_AT_S)
    for pt in scope.points():
        scope.move_to(pt)
        scope.autofocus()
        scope.capture_channels(OVERVIEW_CHANNELS)
