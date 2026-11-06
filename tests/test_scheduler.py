import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import main
from scheduler import add_group_digest_job, add_jobs_digest_job, make_scheduler


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

    def test_group_digest_runs_daily_at_moscow_time_with_overlap_protection(self):
        async def job():
            pass

        scheduler = make_scheduler("UTC")
        add_jobs_digest_job(scheduler, job)
        add_group_digest_job(scheduler, job, "21:00")
        jobs = {item.id: item for item in scheduler.get_jobs()}
        self.assertEqual(set(jobs), {"jobs_spb_1000_utc", "group_digest_daily"})
        group = jobs["group_digest_daily"]
        self.assertEqual(group.max_instances, 1)
        self.assertTrue(group.coalesce)
        self.assertEqual(group.misfire_grace_time, 3600)
        next_run = group.trigger.get_next_fire_time(None, datetime(2026, 11, 7, 17, tzinfo=timezone.utc))
        self.assertEqual(next_run.astimezone(ZoneInfo("Europe/Moscow")), datetime(2026, 11, 7, 21, tzinfo=ZoneInfo("Europe/Moscow")))
        self.assertEqual(next_run.astimezone(timezone.utc).hour, 18)


class MainGroupSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_registration_requires_enabled_configured_source_and_ignores_vacancy_pause(self):
        for enabled, chat in ((True, "@vakifbank_tr"), (True, ""), (False, "@vakifbank_tr")):
            with self.subTest(enabled=enabled, chat=chat):
                cfg = SimpleNamespace(
                    log_level="INFO", tz="Europe/Moscow", telegram_user_enabled=enabled,
                    telegram_source_channels=[], telegram_group_digest_chat=chat,
                    telegram_group_digest_time="21:00",
                )
                app = SimpleNamespace(
                    initialize=AsyncMock(), start=AsyncMock(), stop=AsyncMock(), shutdown=AsyncMock(),
                    updater=SimpleNamespace(start_polling=AsyncMock(), stop=AsyncMock()),
                )
                with (
                    patch.object(main, "load_config", return_value=cfg),
                    patch.object(main, "setup_logging"),
                    patch.object(main, "init_db"),
                    patch.object(main, "build_app", return_value=app),
                    patch.object(main, "make_scheduler", return_value=SimpleNamespace(start=Mock())),
                    patch.object(main, "register_bot_commands", new_callable=AsyncMock),
                    patch.object(main, "add_group_digest_job") as add_group,
                    patch.object(main, "add_jobs_digest_job") as add_jobs,
                    patch.object(main, "get_paused", return_value=True) as paused,
                    patch.object(main, "deliver_group_digest", new_callable=AsyncMock) as deliver,
                    patch.object(main, "asyncio", SimpleNamespace(sleep=AsyncMock(side_effect=asyncio.CancelledError))),
                ):
                    with self.assertRaises(asyncio.CancelledError):
                        await main.main_async()
                    add_jobs.assert_not_called()
                    if enabled and chat:
                        add_group.assert_called_once()
                        self.assertEqual(add_group.call_args.args[2], "21:00")
                        await add_group.call_args.args[1]()
                        deliver.assert_awaited_once_with(app, cfg)
                        paused.assert_not_called()
                    else:
                        add_group.assert_not_called()


if __name__ == "__main__":
    unittest.main()
import asyncio
