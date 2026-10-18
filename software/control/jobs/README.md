# JOBs: scripted acquisition

A **job** is a normal Python file that defines `run_job(scope)`. It says *what* is imaged and *when* (loops over
positions and time points, channels, autofocus, waiting). It never controls hardware directly: everything goes
through the high-level `scope` object, which uses the same tested movement, autofocus and imaging code as the
standard acquisition. The standard acquisition (Flexible Multipoint tab) is unchanged.

## Checking a job graphically

*Show job plan (graph)* in the JOBs tab (or `python -m control.jobs.plot job.py positions.csv --channels BF,488 --out plan.png`
from the `software` folder) runs the dry run and draws what the job would do:

- a **timeline per position** (rows grouped by group; one tick per image, colour = channel; triangles = autofocus; dashed lines = time points, red = started late),
- a **detail** of the first time point (or any range you type in the window) showing the order of channels, autofocus and moves,
- a **stage map** coloured by group with the visiting order (red rings: positions the job never images),
- the **working time per time point** against the interval (shows when the interval is too short),
- a **matrix** of images per channel and autofocus runs for each time point (e.g. "BF only every 4th time point").

The graph needs the Python package `matplotlib` (`pip install matplotlib` in the environment that starts the software; everything else works without it).

Times are estimates (assumed duration of moves and autofocus plus the exposure times); use the graph to check order and
structure, not exact timing. If the job fails, the figure shows everything up to the error.

## Workflow

1. **Flexible Multipoint tab:** define the positions (stage position + optional z). Give them **IDs** in the
   positions table; the **group** of a position is the part of its ID before the first `-`
   (`A-0`, `A-1`, `B-0` ... -> groups `A` and `B`; an empty ID belongs to the group `default`).
2. **JOBs tab:** *Select job...* (or *New from example...*), choose saving folder and experiment ID,
   *Check job (dry run)*, then *Start job*. *Abort job* stops it safely (illumination off).
3. The positions are read from the Flexible Multipoint tab when you press Check/Start.

*Check job* runs your script with simulated time and no hardware. It finds errors (unknown channel, typos), counts
images per channel and group, autofocus runs, and estimates the total duration and late time points. Jobs
that decide on image *content* may behave differently in the dry run (images are dummy arrays).

## The `scope` API

```python
def run_job(scope):
    for t in scope.timepoints(48, interval=1800):      # 30 min from start to start
        for pt in scope.points("A", "B"):              # points of groups A and B, in table order
            scope.move_to(pt)
            if (t.index + pt.index) % 5 == 0:
                scope.autofocus()
            img = scope.capture("488")                 # NumPy array
```

| | |
|---|---|
| `scope.points()` / `scope.points("A", "B")` | list of `Point` in table order (all, or only those groups) |
| `scope.groups` | dict group name -> list of points |
| `Point` | `.id`, `.group`, `.index` (0-based in the table), `.number` (0-based within its group), `.x .y .z` (mm; z is updated after an autofocus) |
| `scope.timepoints(n, interval=None)` | loop; yields `TimePoint` with `.index`, `.start` (s since job start), `.late` (s). `n=None`: until aborted |
| `scope.move_to(point)` | move the stage (same backlash/safety handling as the standard acquisition) |
| `scope.autofocus(channel=None)` | contrast autofocus with the settings of the Autofocus tab; stores the focused z in the point; returns z (mm) |
| `scope.capture("488")` | one image in a channel; saved automatically; returns the image as NumPy array. `save=False`: do not save |
| `scope.capture_channels(["BF", "488"])` | several channels in that order; returns `{channel: image}` |
| `scope.wait(seconds)` | wait (abortable) |
| `scope.wait_until(seconds)` | wait until this many seconds after the job start |
| `scope.elapsed` | seconds since the job start |
| `scope.channels` | available channel names |
| `scope.log(text)`, `scope.warn(text)` | message in the JOBs tab and in `job_log.txt` |
| `scope.abort_requested` | True after the user pressed Abort (API calls stop the job anyway) |

**Channels** are named as in the channel configuration. Any unique part of the name, case-insensitive, works
(`"488"` for `Fluorescence 488 nm Ex`); ambiguous or unknown names are errors that list the choices.
Exposure, gain and intensity come from the channel configuration.

**Time points:** `interval` is the time from the *start* of one time point to the *start* of the next. If a time
point takes longer than the interval, the next one starts **immediately** (none is skipped), the delay is logged
and the following interval counts from that start.

**Abort:** *Abort job* raises an exception inside the job at the next `scope` call. It is not caught by
`except Exception:` in your script. Afterwards the illumination is off and the files are complete up to that point.

**Errors in the job:** the runner reports them (with the line in your file), turns the illumination off and
ends the experiment cleanly. Errors found by the dry run prevent the start.

## What is written to the experiment folder

| file | content |
|---|---|
| `t0000/`, `t0001/`, ... | images of each time point (`no_timepoint/` for images outside `scope.timepoints`), named `p<index>_<id>_<channel>.tiff` (a number is appended if the same image name occurs twice); OME-TIFF as in the standard acquisition |
| `images.csv` | one row per image: file, time since start, wall time, time point, point index/ID/group, channel, actual stage x/y/z, exposure, gain, intensity |
| `autofocus.csv` | one row per autofocus: time, point, channel, z before/after, duration |
| `job.py` | copy of the job file that was run |
| `job_log.txt` | what happened and when (incl. late time points, warnings) |
| `job_meta.json` | status (completed/aborted/failed), duration, counts, warnings, software commit |
| `positions_initial.csv`, `positions_final.csv` | positions before the job and with the z values after autofocus |
| `configurations.xml`, `acquisition parameters.json` | as in the standard acquisition |

## Limits of this first version

* XY points with several channels, autofocus, scheduling. No Z stacks, movies, tiled scans (NX x NY) yet;
  RGB and USB-spectrometer channels are not available.
* **Known issue:** with channels of *different exposure times* the camera may use the previous channel's exposure
  after a channel switch (see `tests/diagnose_exposure.py` on the `squid-improvements` branch). Until it is resolved,
  use the same exposure for all channels of a job; the check reports a warning otherwise.
* No napari / tiled-preview updates during a job; the live image display is updated.
* Image saving is synchronous (as in the standard acquisition).
* The job file is arbitrary Python, so it can do anything Python can; `scope` is the only supported way to control the microscope.

Examples: `software/job_examples/`.  Tests: `tests/test_jobs_api.py` (no hardware), `tests/e2e_jobs.py` (simulation or real microscope).
