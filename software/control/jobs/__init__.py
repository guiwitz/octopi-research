"""JOBs: scripted acquisition. Job files define run_job(scope); see control/jobs/README.md."""
from control.jobs.api import Scope, Point, TimePoint, JobError, JobAborted, make_points
from control.jobs.runner import run_job, dry_run, describe, load_job, job_documentation, JobResult
