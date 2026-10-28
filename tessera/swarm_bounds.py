"""Effective swarm bounds. A saved value holds in both modes.

``max_job_tasks`` omitted: Safe mode allows eight workers in one job record.
YOLO mode does not add that cap. A saved integer of at least 2 holds in both.

``distinct_workers`` omitted: Safe mode requires each worker in a job to use
its own backend and its own account, so the job's evidence is independent.
That is a guard on the shape of the job. It is not a rule that a judge must
differ from a worker. Overlapping scopes are refused either way.
YOLO mode drops the guard. A saved true or false holds in both modes.
"""

from .endpoint_limit import yolo_mode

# Safe mode only, when max_job_tasks is omitted. Unleashed does not add this cap.
# A number set by hand replaces it in both modes.
SAFE_JOB_TASK_LIMIT = 8


def job_task_limit(config):
    """How many workers one job may list, or None when the job has no size cap."""
    explicit = config.swarm.max_job_tasks
    if explicit is not None and explicit != '':
        if isinstance(explicit, str):
            if not explicit.isdigit():
                raise ValueError('max_job_tasks must be an integer of at least 2, or omitted')
            explicit = int(explicit)
        if type(explicit) is not int or isinstance(explicit, bool) or explicit < 2:
            raise ValueError('max_job_tasks must be an integer of at least 2, or omitted')
        return explicit
    if yolo_mode(config):
        return None
    return SAFE_JOB_TASK_LIMIT


def require_distinct_workers(config):
    """Whether one job must give each worker its own backend and account."""
    explicit = config.swarm.distinct_workers
    if explicit is None or explicit == '':
        return not yolo_mode(config)
    if explicit is True or explicit == 'true':
        return True
    if explicit is False or explicit == 'false':
        return False
    raise ValueError('swarm.distinct_workers must be true, false, or omitted')
