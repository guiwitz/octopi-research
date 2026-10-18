"""Simple time lapse: every 10 minutes, visit all points and take one image in each of two channels.

Channel names: use the names of your channel configuration, or any unique part of it (case-insensitive),
e.g. "488" for "Fluorescence 488 nm Ex". A part that matches several channels (such as "BF") is an error: use the full name, e.g. "BF LED matrix full". scope.channels lists them.
"""
CHANNELS = ["BF LED matrix full", "488"]


def run_job(scope):
    for t in scope.timepoints(12, interval=600):
        for pt in scope.points():
            scope.move_to(pt)
            for ch in CHANNELS:
                scope.capture(ch)
