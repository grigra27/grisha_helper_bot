import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import main
from scheduler import add_jobs_digest_job, make_scheduler


class SchedulerTests(unittest.TestCase):
    def test_only_vacancies_are_scheduled_including_weekends(self):
        async def job():
            pass

        scheduler = make_scheduler("Europe/Moscow")
        add_jobs_digest_job(scheduler, job)
        jobs = scheduler.get_jobs()
        self.assertEqual([item.id for item in jobs], ["jobs_spb_1000_utc"])
        self.assertIs(jobs[0].func, job)
        # Friday after the run: the next delivery must be Saturday at 13:00 MSK.
        next_run = jobs[0].trigger.get_next_fire_time(None, datetime(2026, 11, 6, 12, tzinfo=timezone.utc))
        self.assertEqual(next_run.astimezone(ZoneInfo("Europe/Moscow")), datetime(2026, 11, 7, 13, tzinfo=ZoneInfo("Europe/Moscow")))
        self.assertTrue(callable(main.main_async))


if __name__ == "__main__":
    unittest.main()
