"""Time lapse every minute: BF + GFP + RFP, autofocus (RFP, GLVA) at the first time point and then staggered (template)."""

# ---- settings: edit these ------------------------------------------------------------------
CHANNELS = ["BF LED matrix full", "488", "561"]   # imaged in this order at every position: BF, GFP (488), RFP (561)
AF_CHANNEL = "561"                                # RFP is used for the autofocus
AF_FOCUS_MEASURE = "GLVA"                         # focus measure for the autofocus of this job (the Autofocus tab keeps its own setting)
AF_EVERY = 5                                      # after the first time point, each position is refocused every AF_EVERY-th time point
N_TIMEPOINTS = 120
INTERVAL_S = 60                                   # 1 min from the START of one time point to the START of the next


def run_job(scope):
    """WHAT IT DOES: all positions of the Flexible Multipoint table are treated as ONE group (IDs / groups are ignored).
    Every INTERVAL_S seconds each position is visited in table order and imaged in CHANNELS (BF, GFP, RFP).
    Autofocus (channel AF_CHANNEL = RFP, focus measure AF_FOCUS_MEASURE = GLVA) runs
      - at the FIRST time point at ALL positions, and
      - afterwards staggered: a position with table index k is refocused at the time points t >= 1 where (t + k) is a
        multiple of AF_EVERY, i.e. every AF_EVERY-th time point, and only 1/AF_EVERY of the positions per time point.
    The focused z is remembered for each position, so the next autofocus starts around it. If a pass takes longer than
    the interval, the next time point starts immediately (none is skipped).

    YOU PROVIDE:
      - Positions in the Flexible Multipoint tab (stage x, y and a start z close to focus; IDs / groups are not needed).
      - The channel names in CHANNELS and AF_CHANNEL must match the channel configuration of your Squid
        ("488" = GFP and "561" = RFP match the Fluorescence 488 / 561 channels; edit if yours are named differently).
      - Number of planes and step of the autofocus: Autofocus tab (the focus measure and channel are set by this job).
      - Exposure, intensity and gain come from the channel configuration. Use the same exposure for all channels
        (the check run warns otherwise, see the README). Check that one pass fits into INTERVAL_S (Show job plan).
    """
    for t in scope.timepoints(N_TIMEPOINTS, interval=INTERVAL_S):
        for pt in scope.points():                                      # one group: all positions, table order
            scope.move_to(pt)
            if t.index == 0 or (t.index + pt.index) % AF_EVERY == 0:   # first time point: all; later: staggered
                scope.autofocus(channel=AF_CHANNEL, focus_measure=AF_FOCUS_MEASURE)
            for ch in CHANNELS:
                scope.capture(ch)
