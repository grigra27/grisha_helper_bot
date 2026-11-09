import asyncio
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
from telegram_jobs import ChannelRunStats


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


class MainVacancyDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cfg = SimpleNamespace(
            log_level="INFO", tz="Europe/Moscow", telegram_user_enabled=True,
            telegram_source_channels=["@Perforum_jobs"], telegram_group_digest_chat="",
            telegram_group_digest_time="21:00",
        )
        self.app = SimpleNamespace(
            initialize=AsyncMock(), start=AsyncMock(), stop=AsyncMock(), shutdown=AsyncMock(),
            updater=SimpleNamespace(start_polling=AsyncMock(), stop=AsyncMock()),
        )
        self.events = []

        async def send(app, cfg, text):
            self.events.append(("send", text))

        self.commit = Mock(side_effect=lambda: self.events.append("commit"))
        for name, value in (
            ("load_config", self.cfg),
            ("build_app", self.app),
            ("make_scheduler", SimpleNamespace(start=Mock())),
            ("get_paused", False),
        ):
            self.enterContext(patch.object(main, name, return_value=value))
        self.enterContext(patch.object(main, "setup_logging"))
        self.enterContext(patch.object(main, "init_db"))
        self.enterContext(patch.object(main, "register_bot_commands", new_callable=AsyncMock))
        self.enterContext(patch.object(main, "add_group_digest_job"))
        self.enterContext(patch.object(
            main, "asyncio", SimpleNamespace(sleep=AsyncMock(side_effect=asyncio.CancelledError))
        ))
        self.run = self.enterContext(patch.object(main, "run_spb_jobs_digest", new_callable=AsyncMock))
        self.send = self.enterContext(patch.object(main, "send_to_owner", new_callable=AsyncMock, side_effect=send))
        scheduled = self.enterContext(patch.object(main, "add_jobs_digest_job"))
        with self.assertRaises(asyncio.CancelledError):
            await main.main_async()
        scheduled.assert_called_once()
        self.job = scheduled.call_args.args[1]

    async def test_empty_vacancies_send_one_report_with_statistics_once_then_commit(self):
        stats = [ChannelRunStats("@Perforum_jobs", "Perforum Jobs", 150, 25, 0)]
        text = "В новых постах вакансий СПб и удалённой работы не найдено.\n\n" + main.format_channel_stats(stats)
        self.run.return_value = (text, 0, stats, self.commit)

        await self.job()

        self.send.assert_awaited_once_with(self.app, self.cfg, text)
        self.assertEqual(text.count("Статистика по каналам:"), 1)
        self.assertEqual(self.events, [("send", text), "commit"])
        self.commit.assert_called_once_with()

    async def test_nonempty_vacancies_send_one_report_with_count_and_statistics_then_commit(self):
        stats = [ChannelRunStats("@Perforum_jobs", "Perforum Jobs", 150, 25, 3)]
        text = "Вакансии Санкт-Петербурга:\nАналитик\nhttps://perforum.io/jobs/123"
        self.run.return_value = (text, 2, stats, self.commit)

        await self.job()

        self.send.assert_awaited_once()
        report = self.send.await_args.args[2]
        self.assertTrue(report.startswith(text))
        self.assertIn("Подходящих постов: 2.", report)
        self.assertEqual(report.count("Статистика по каналам:"), 1)
        self.assertIn("выбрано 3", report)
        self.assertEqual(self.events, [("send", report), "commit"])
        self.commit.assert_called_once_with()

    async def test_failed_vacancy_report_send_does_not_commit(self):
        stats = [ChannelRunStats("@Perforum_jobs", "Perforum Jobs", 1, 1, 1)]
        self.run.return_value = ("Вакансия аналитика", 1, stats, self.commit)
        self.send.side_effect = [RuntimeError("Telegram unavailable"), None]

        with self.assertLogs(main.__name__, level="ERROR"):
            await self.job()

        self.commit.assert_not_called()
        self.assertNotIn("commit", self.events)
        self.assertEqual(self.send.await_count, 2)
        self.assertIn("Вакансия аналитика", self.send.await_args_list[0].args[2])
        self.assertTrue(self.send.await_args_list[1].args[2].startswith("Ошибка авто-сбора вакансий СПб:"))


if __name__ == "__main__":
    unittest.main()
