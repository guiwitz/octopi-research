"""Two phases: first a dense time lapse of group "A" (every 5 min for 1 h), then wait, then one overview of all points.
Shows scope.wait_until (seconds since the job started) and use of the returned image.
"""


def run_job(scope):
    for t in scope.timepoints(12, interval=300):
        for pt in scope.points("A"):
            scope.move_to(pt)
            image = scope.capture("488")
            if image.max() == 0:                          # images are NumPy arrays: decisions can depend on content
                scope.warn(f"empty image at {pt.id}")
    scope.wait_until(2 * 3600)                            # 2 h after the start of the job
    for pt in scope.points():
        scope.move_to(pt)
        scope.autofocus()
        scope.capture_channels(["BF", "488"])
