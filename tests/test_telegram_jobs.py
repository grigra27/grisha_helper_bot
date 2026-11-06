import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.append(str(Path(__file__).resolve().parents[1] / "app"))

import unittest

import telegram_jobs
from telegram_jobs import _effective_source_fetch_limit


class TelegramJobsTests(unittest.TestCase):
    def test_effective_source_fetch_limit_keeps_positive_value(self):
        self.assertEqual(_effective_source_fetch_limit(120), 120)

    def test_effective_source_fetch_limit_fallback_for_invalid_values(self):
        self.assertEqual(_effective_source_fetch_limit(0), 80)
        self.assertEqual(_effective_source_fetch_limit(-5), 80)
        self.assertEqual(_effective_source_fetch_limit("bad"), 80)
        self.assertEqual(_effective_source_fetch_limit(None), 80)

    def test_effective_source_fetch_limit_caps_oversized_values(self):
        self.assertEqual(_effective_source_fetch_limit(9999), 300)


def post(message_id, url=None, text="Вакансия"):
    return SimpleNamespace(
        id=message_id,
        message=text,
        date=None,
        entities=[SimpleNamespace(url=url)] if url else [],
    )


def job(url, title="Аналитик", *, spb=False, remote=False):
    return SimpleNamespace(
        title=title, link=url, company="Работодатель", location="Город",
        is_spb=spb, is_remote=remote,
    )


class FakeTelegram:
    def __init__(self, messages):
        self.messages = messages
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def is_user_authorized(self):
        return True

    async def get_entity(self, reference):
        index = list(self.messages).index(reference)
        return SimpleNamespace(id=100 + index, title=reference)

    async def get_messages(self, entity, **kwargs):
        self.calls.append((entity.title, kwargs))
        return self.messages[entity.title]


class JobsIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg = SimpleNamespace(
            telegram_user_enabled=True,
            telegram_user_session="unused",
            telegram_user_api_id=1,
            telegram_user_api_hash="unused",
            telegram_source_channels=["@jobs"],
            telegram_source_fetch_limit=150,
            telegram_vacancy_banned_words=("водитель", "медицин"),
        )
        self.fake = FakeTelegram({"@jobs": []})
        self.factory = self.enterContext(patch.object(telegram_jobs, "TelegramClient", return_value=self.fake))
        self.enterContext(patch.object(telegram_jobs, "StringSession", return_value=None))
        self.last_id = self.enterContext(patch.object(telegram_jobs, "get_tg_source_last_id", return_value=0))
        self.write = self.enterContext(patch.object(telegram_jobs, "set_tg_source_last_id"))
        self.fetch = self.enterContext(patch.object(telegram_jobs, "fetch_perforum_job", new_callable=AsyncMock))
        # URL extraction and the site parser have separate adapter tests. Here the
        # entities represent canonical candidates independent of Telegram framing.
        self.enterContext(patch.object(
            telegram_jobs, "extract_perforum_links",
            side_effect=lambda text, entities: list(dict.fromkeys(
                entity.url.split("?", 1)[0] for entity in entities or [] if entity.url
            )),
        ))

    async def test_perforum_routes_filters_deduplicates_and_defers_cursor(self):
        urls = [f"https://perforum.io/jobs/{i}" for i in range(1, 8)]
        jobs = {
            urls[0]: job(urls[0], "Офис СПб", spb=True),
            urls[1]: job(urls[1], "Удалённый аналитик", remote=True),
            urls[2]: job(urls[2], "СПб и удалёнка", spb=True, remote=True),
            urls[3]: job(urls[3], "Москва"),
            urls[4]: job(urls[4], "Неизвестная география"),
            urls[5]: job(urls[5], "МЕДИЦИНСКИЙ консультант", spb=True),
            urls[6]: None,
        }
        self.fake.messages["@jobs"] = [post(8, urls[1] + "?tracking=two")] + [
            post(i, url + "?tracking=one") for i, url in reversed(list(enumerate(urls, start=1)))
        ]
        self.fetch.side_effect = lambda client, url: jobs[url]
        text, count, stats, commit = await telegram_jobs.run_spb_jobs_digest(self.cfg)
        self.assertEqual(count, 3)
        self.assertEqual(stats[0].fetched_posts, 8)
        self.assertEqual(stats[0].detected_vacancies, 7)
        self.assertEqual(stats[0].selected_vacancies, 3)
        for url in urls[:3]:
            self.assertEqual(text.count(url), 1)
        self.assertNotIn("Москва", text)
        self.assertNotIn("МЕДИЦИНСКИЙ", text)
        self.assertNotIn("Неизвестная география", text)
        self.assertGreater(text.index("СПб и удалёнка"), text.index("удаленная работа:"))
        self.assertEqual(self.fetch.await_count, 7)
        self.assertEqual(self.fake.calls[0][1], {"limit": 150, "min_id": 0, "reverse": False})
        self.write.assert_not_called()
        commit()
        self.write.assert_called_once_with("100", 8)
        commit()
        self.write.assert_called_once()

    async def test_legacy_hh_format_needs_no_web_client_and_also_defers_cursor(self):
        self.fake.messages["@jobs"] = [post(50, text=(
            "Компания: Страхование\nСанкт-Петербург\n"
            "1. Специалист — ЗП не указана\nСсылка: https://hh.ru/vacancy/123"
        ))]
        with patch.object(telegram_jobs.httpx, "AsyncClient", side_effect=AssertionError("No web needed")):
            text, count, stats, commit = await telegram_jobs.run_spb_jobs_digest(self.cfg)
        self.assertEqual(count, 1)
        self.assertIn("https://hh.ru/vacancy/123", text)
        self.assertEqual(stats[0].selected_vacancies, 1)
        self.fetch.assert_not_awaited()
        self.write.assert_not_called()
        commit()
        self.write.assert_called_once_with("100", 50)

    async def test_incremental_fetch_uses_oldest_first_and_never_rolls_back_cursor(self):
        self.last_id.return_value = 10
        self.fake.messages["@jobs"] = [post(12), post(11)]
        _, _, _, commit = await telegram_jobs.run_spb_jobs_digest(self.cfg)
        self.assertEqual(self.fake.calls[0][1], {"limit": 150, "min_id": 10, "reverse": True})
        self.write.assert_not_called()
        self.last_id.return_value = 15  # Another completed run already advanced farther.
        commit()
        self.write.assert_called_once_with("100", 15)

    async def test_page_failure_leaves_all_channel_cursors_untouched(self):
        self.cfg.telegram_source_channels = ["@jobs", "@other"]
        self.fake.messages = {
            "@jobs": [post(40)],
            "@other": [post(41, "https://perforum.io/jobs/failed")],
        }
        self.fetch.side_effect = RuntimeError("Page unavailable")
        with self.assertRaisesRegex(RuntimeError, "Page unavailable"):
            await telegram_jobs.run_spb_jobs_digest(self.cfg)
        self.write.assert_not_called()

    async def test_cross_channel_cache_and_dedup_fetch_a_page_once(self):
        url = "https://perforum.io/jobs/same"
        self.cfg.telegram_source_channels = ["@jobs", "@other"]
        self.fake.messages = {"@jobs": [post(1, url + "?one=1")], "@other": [post(2, url + "?two=2")]}
        self.fetch.return_value = job(url, spb=True)
        text, count, _, commit = await telegram_jobs.run_spb_jobs_digest(self.cfg)
        self.assertEqual(count, 1)
        self.assertEqual(text.count(url), 1)
        self.fetch.assert_awaited_once()
        self.write.assert_not_called()
        commit()
        self.assertEqual(self.write.call_count, 2)

    async def test_page_loading_is_bounded_to_three_concurrent_requests(self):
        self.fake.messages["@jobs"] = [post(i, f"https://perforum.io/jobs/{i}") for i in range(1, 10)]
        active, maximum = 0, 0

        async def fetch(client, url):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            try:
                await asyncio.sleep(0.001)
                return job(url, spb=True)
            finally:
                active -= 1

        self.fetch.side_effect = fetch
        await telegram_jobs.run_spb_jobs_digest(self.cfg)
        self.assertEqual(maximum, 3)
        self.write.assert_not_called()

    async def test_disabled_source_returns_noop_commit_without_connections(self):
        self.cfg.telegram_user_enabled = False
        text, count, stats, commit = await telegram_jobs.run_spb_jobs_digest(self.cfg)
        self.assertIn("отключён", text)
        self.assertEqual((count, stats), (0, []))
        commit()
        self.factory.assert_not_called()
        self.write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
