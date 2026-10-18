"""Simple time lapse (example job)."""

# ---- settings: edit these ------------------------------------------------------------------
CHANNELS = ["BF LED matrix full", "488"]    # channels imaged at every position (names from the channel configuration)
N_TIMEPOINTS = 12                            # number of time points
INTERVAL_S = 600                             # seconds from the START of one time point to the START of the next


def run_job(scope):
    """WHAT IT DOES: every INTERVAL_S seconds, visits all positions in the order of the Flexible Multipoint table and takes
    one image per channel in CHANNELS at each. Repeats N_TIMEPOINTS times. If a pass takes longer than the interval,
    the next time point starts immediately (none is skipped).

    YOU PROVIDE:
      - Positions: define them in the Flexible Multipoint tab (IDs are optional for this job).
      - Settings at the top of this file: CHANNELS, N_TIMEPOINTS, INTERVAL_S.
      - Channel names: the full name from the channel configuration, or a part that matches exactly one channel
        (e.g. "488"). "BF" alone matches several channels and is refused.
      - Exposure, intensity and gain are taken from the channel settings; use the same exposure for all channels.
    """
    for t in scope.timepoints(N_TIMEPOINTS, interval=INTERVAL_S):
        for pt in scope.points():
            scope.move_to(pt)
            for ch in CHANNELS:
                scope.capture(ch)
