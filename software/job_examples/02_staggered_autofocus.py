"""Time lapse with staggered autofocus and brightfield every 4th time point (example job)."""

# ---- settings: edit these ------------------------------------------------------------------
FLUORESCENCE = "488"                  # imaged at every position and time point
BRIGHTFIELD = "BF LED matrix full"    # imaged only every BF_EVERY-th time point
BF_EVERY = 4
GROUPS = ("A", "B")                   # position groups to image (part of the position ID before the first "-")
AF_EVERY = 5                          # every position is refocused at every AF_EVERY-th time point (staggered)
N_TIMEPOINTS = 48
INTERVAL_S = 1800                     # 30 min between time-point starts


def run_job(scope):
    """WHAT IT DOES: images all positions of the groups in GROUPS at every time point (fluorescence), adds a brightfield
    image every BF_EVERY-th time point, and runs the autofocus at only 1/AF_EVERY of the positions per time point:
    the position with table index k is focused at the time points where (time point + k) is a multiple of AF_EVERY, so every position
    is refocused regularly but the autofocus time is spread out. The focused z is remembered for later visits.

    YOU PROVIDE:
      - Positions in the Flexible Multipoint tab, with IDs that start with the group name, e.g. A-0, A-1, ..., B-0, B-1, ...
        (the group is the part of the ID before the first "-"). Groups not listed in GROUPS are ignored.
      - Settings at the top of this file (channels, groups, how often, number of time points, interval).
      - The autofocus uses the settings of the Autofocus tab (focus measure, number of planes, step).
    """
    for t in scope.timepoints(N_TIMEPOINTS, interval=INTERVAL_S):
        for pt in scope.points(*GROUPS):
            scope.move_to(pt)
            if (t.index + pt.index) % AF_EVERY == 0:
                scope.autofocus()
            scope.capture(FLUORESCENCE)
            if t.index % BF_EVERY == 0:
                scope.capture(BRIGHTFIELD)
