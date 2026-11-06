import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)


def make_scheduler(timezone: str) -> AsyncIOScheduler:
    return AsyncIOScheduler(timezone=timezone)


def add_jobs_digest_job(scheduler: AsyncIOScheduler, job_func):
    scheduler.add_job(
        job_func,
        trigger=CronTrigger(hour=10, minute=0, timezone="UTC"),
        id="jobs_spb_1000_utc",
        replace_existing=True,
    )
    logger.info("Scheduled SPB jobs digest at 10:00 UTC daily (13:00 Europe/Moscow)")
