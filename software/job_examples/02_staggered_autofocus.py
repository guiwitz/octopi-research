"""Fluorescence at every point and time point, brightfield every 4th time point, autofocus staggered so that
every point is focused at every 5th time point but only 1/5 of the points per time point.
Point groups: images of group "A" and "B" are taken at different rates.
Position IDs are typed in the positions table of the Flexible Multipoint tab, e.g. A-0, A-1, ..., B-0, B-1 ...
(the group is the part before the first '-').
"""
FLUORESCENCE = "488"
BRIGHTFIELD = "BF LED matrix full"


def run_job(scope):
    for t in scope.timepoints(48, interval=1800):          # 30 min between time-point starts
        scope.log(f"time point {t.index}")
        for pt in scope.points("A", "B"):
            scope.move_to(pt)
            if (t.index + pt.index) % 5 == 0:             # staggered: 20 % of the points per time point
                scope.autofocus()
            scope.capture(FLUORESCENCE)
            if t.index % 4 == 0:
                scope.capture(BRIGHTFIELD)
        # group B only every second time point would be:  if t.index % 2 == 0: for pt in scope.points("B"): ...
