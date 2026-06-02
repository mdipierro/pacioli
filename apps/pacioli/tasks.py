"""Background scheduler placeholder (disabled by default).

A real deployment would run webhook delivery from the ``event`` outbox here.
"""

from . import settings
from .common import db, logger

scheduler = None

if settings.USE_SCHEDULER:
    from pydal.tools.scheduler import Scheduler

    scheduler = Scheduler(
        db, logger=logger, max_concurrent_runs=settings.SCHEDULER_MAX_CONCURRENT_RUNS
    )
    scheduler.start()
