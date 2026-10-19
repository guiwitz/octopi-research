"""A 5 s movie at 25 frames/s (2000 x 2000 pixels, 8 bit, uncompressed) at every position, then a normal 16-bit image."""

CHANNEL = "488"
FPS = 25
DURATION_S = 5


def run_job(scope):
    """WHAT IT DOES: at every position: a video-rate movie in 488 (camera free running, light on only while recording, frames collected in
    memory and written afterwards as Time0000_Point0000_FastMovie.tiff), then a normal 16-bit image of the same channel. The camera settings
    (8 bit, 2000 x 2000 region, exposure, frame rate) are restored after the movie.

    YOU PROVIDE: positions in the Flexible Multipoint tab; a channel whose exposure is shorter than 1/FPS (40 ms); an intensity that does not
    saturate the 8-bit image (the log warns). A 40 s movie of this size is the most that fits in the 4 GB buffer."""
    for pt in scope.points():
        scope.move_to(pt)
        scope.fast_movie(CHANNEL, fps=FPS, duration_s=DURATION_S, roi=2000, bits=8)
        scope.capture(CHANNEL)
