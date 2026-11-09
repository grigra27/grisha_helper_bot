import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from openai import APIStatusError, RateLimitError
from telethon.tl.types import MessageEntityTextUrl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import db
import press_monitor as press
from test_group_digest import FakeTelegram, message

SOURCE_ID = 1110709402
CURSOR = f"tg_press_last_id:{SOURCE_ID}"


def linked_post(identifier, queries, *, prefix=""):
    text, entities = prefix, []
    for title, url in queries:
        start = len(text.encode("utf-16-le")) // 2
        text += title
        entities.append(MessageEntityTextUrl(start, len(title.encode("utf-16-le")) // 2, url))
        text += "\nТестовое СМИ · 10 ноября\n\n"
    text += "Хотите получать запросы на вашу тему? Используйте бота Pressfeed."
    result = message(identifier, text)
    result.entities = entities
    return result


class FakePressTelegram(FakeTelegram):
    async def get_entity(self, reference):
        return SimpleNamespace(id=SOURCE_ID, title="Запросы СМИ", username="Pressfeed_queries")


class FakeClassifier:
    def __init__(self):
        self.payloads, self.failures = [], []
        self.pick = lambda item: False
        self.corrupt = lambda rows: rows
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def with_options(self, **kwargs):
        return self

    def close(self):
        pass

    def create(self, **kwargs):
        items = json.loads(kwargs["messages"][-1]["content"])["requests"]
        self.payloads.append(items)
        if self.failures:
            raise self.failures.pop(0)
        rows = []
        for item in items:
            relevant = self.pick(item)
            rows.append({"id": item["id"], "part": item["part"], "relevant": relevant,
                         "category": "insurance" if relevant else None,
                         "reason": "Нужен комментарий о страховании" if relevant else ""})
        content = json.dumps({"results": self.corrupt(rows)}, ensure_ascii=False)
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=content))])


class PressAdapterTests(unittest.TestCase):
    def test_separates_queries_using_utf16_links_and_keeps_source_metadata(self):
        post = linked_post(1, [
            ("📰 Кредиты и страхование", "https://pressfeed.ru/query/12?utm_source=telegram"),
            ("Виды икры", "https://pressfeed.ru/query/13"),
        ], prefix="🔥 Подборка\n")
        requests = press.extract_requests(post)
        self.assertEqual([request.title for request in requests], ["📰 Кредиты и страхование", "Виды икры"])
        self.assertEqual(requests[0].url, "https://pressfeed.ru/query/12")
        self.assertEqual(requests[0].outlet, "Тестовое СМИ")
        self.assertEqual(requests[0].date_text, "10 ноября")
        self.assertNotIn("Виды икры", requests[0].text)
        self.assertNotIn("Используйте бота", requests[1].text)

    def test_query_urls_are_canonical_and_external_urls_are_not_treated_as_requests(self):
        for url in ["http://pressfeed.ru/query/1", "https://pressfeed.ru.evil.test/query/1", "https://pressfeed.ru:444/query/1", "https://user@pressfeed.ru/query/1", "https://t.me/pressfeed_requests_bot"]:
            self.assertIsNone(press.canonical_pressfeed_url(url))
        self.assertEqual(press.canonical_pressfeed_url("https://www.pressfeed.ru/query/12/?utm=one#x"), "https://pressfeed.ru/query/12")

    def test_duplicate_tracking_links_are_one_request(self):
        post = linked_post(1, [("Первый", "https://pressfeed.ru/query/12?utm=one"), ("Повтор", "https://pressfeed.ru/query/12?utm=two")])
        self.assertEqual(len(press.extract_requests(post)), 1)

    def test_moscow_window_includes_0900_and_2100_and_supports_round_the_clock(self):
        cfg = SimpleNamespace(telegram_press_active_hours="09:00-21:00")
        for hour, minute, active in [(5, 59, False), (6, 0, True), (18, 0, True), (18, 1, False)]:
            self.assertEqual(press.monitoring_window_open(cfg, datetime(2026, 11, 9, hour, minute, tzinfo=timezone.utc)), active)
        cfg.telegram_press_active_hours = "00:00-23:59"
        self.assertTrue(press.monitoring_window_open(cfg, datetime(2026, 11, 9, 22, tzinfo=timezone.utc)))


class PressMonitorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(db, "DB_PATH", Path(directory) / "state.db"))
        db.init_db()
        self.cfg = SimpleNamespace(telegram_user_enabled=True, telegram_press_channel="@Pressfeed_queries",
            telegram_user_session="test-session", telegram_user_api_id=1, telegram_user_api_hash="test-hash",
            llm_api_key="test-key", llm_base_url="https://api.groq.com/openai/v1", llm_model="test-model")
        self.telegram = FakePressTelegram([])
        self.llm = FakeClassifier()
        self.send = AsyncMock()
        self.enterContext(patch.object(press, "TelegramClient", return_value=self.telegram))
        self.enterContext(patch.object(press, "StringSession", return_value=None))
        self.enterContext(patch.object(press, "make_client", return_value=self.llm))

    async def test_only_matching_query_from_a_mixed_post_is_delivered(self):
        self.telegram.messages = [linked_post(10, [("Виды икры", "https://pressfeed.ru/query/1"), ("Стоимость ОСАГО", "https://pressfeed.ru/query/2")])]
        self.llm.pick = lambda item: "ОСАГО" in item["text"]
        stats = await press.run_press_monitor(self.cfg, self.send)
        self.assertEqual((stats.posts, stats.checked, stats.sent), (1, 2, 1))
        self.send.assert_awaited_once()
        text = self.send.await_args.args[0]
        self.assertIn("Стоимость ОСАГО", text)
        self.assertNotIn("Виды икры", text)
        self.assertIn("https://pressfeed.ru/query/2", text)
        self.assertIn("https://t.me/Pressfeed_queries/10", text)
        self.assertIn("Дата в подборке: 10 ноября", text)
        self.assertEqual(db.kv_get(CURSOR), "10")

    async def test_no_matches_or_new_posts_is_silent_and_checkpoints_checked_posts(self):
        self.telegram.messages = [linked_post(10, [("Виды икры", "https://pressfeed.ru/query/1")])]
        stats = await press.run_press_monitor(self.cfg, self.send)
        self.assertEqual(stats.checked, 1)
        self.send.assert_not_awaited()
        self.assertEqual(db.kv_get(CURSOR), "10")
        stats = await press.run_press_monitor(self.cfg, self.send)
        self.assertEqual(stats.posts, 0)
        self.send.assert_not_awaited()

    async def test_persistent_query_dedup_works_across_posts_and_runs(self):
        self.llm.pick = lambda item: True
        self.telegram.messages = [linked_post(10, [("Страхование", "https://pressfeed.ru/query/1?utm=one")])]
        await press.run_press_monitor(self.cfg, self.send)
        calls = len(self.llm.payloads)
        self.telegram.messages.append(linked_post(11, [("Страхование снова", "https://pressfeed.ru/query/1?utm=two")]))
        stats = await press.run_press_monitor(self.cfg, self.send)
        self.assertEqual(stats.duplicates, 1)
        self.assertEqual(len(self.llm.payloads), calls)
        self.send.assert_awaited_once()
        self.assertEqual(db.kv_get(CURSOR), "11")

    async def test_retry_after_second_alert_failure_does_not_repeat_the_first(self):
        self.llm.pick = lambda item: True
        self.telegram.messages = [linked_post(10, [("Страхование", "https://pressfeed.ru/query/1"), ("Фонды", "https://pressfeed.ru/query/2")])]
        self.send.side_effect = [None, RuntimeError("delivery failed")]
        with self.assertRaises(RuntimeError):
            await press.run_press_monitor(self.cfg, self.send)
        self.assertIsNone(db.kv_get(CURSOR))
        self.send.reset_mock(); self.send.side_effect = None
        stats = await press.run_press_monitor(self.cfg, self.send)
        self.assertEqual((stats.duplicates, stats.sent), (1, 1))
        self.send.assert_awaited_once()
        self.assertIn("query/2", self.send.await_args.args[0])
        self.assertEqual(db.kv_get(CURSOR), "10")

    async def test_provider_failure_keeps_cursor_and_stable_first_run_boundary(self):
        self.telegram.messages = [linked_post(10, [("Страхование", "https://pressfeed.ru/query/1")])]
        self.llm.failures = [RuntimeError("private-key-marker")]
        with self.assertRaises(press.PressMonitorError) as failure:
            await press.run_press_monitor(self.cfg, self.send)
        self.assertNotIn("private-key-marker", str(failure.exception))
        self.assertIsNone(db.kv_get(CURSOR))
        anchor = db.kv_get(f"tg_press_bootstrap_since:{SOURCE_ID}")
        await press.run_press_monitor(self.cfg, self.send)
        self.assertEqual(self.telegram.iterations[0]["offset_date"], self.telegram.iterations[1]["offset_date"])
        self.assertIsNotNone(anchor)

    async def test_missing_or_fabricated_classification_rows_cannot_checkpoint(self):
        self.telegram.messages = [linked_post(10, [("Страхование", "https://pressfeed.ru/query/1")])]
        for corrupt in (lambda rows: [], lambda rows: [{**rows[0], "id": 999}], lambda rows: [{**rows[0], "relevant": "yes"}]):
            self.llm.corrupt = corrupt
            with self.assertRaises(press.PressMonitorError):
                await press.run_press_monitor(self.cfg, self.send)
            self.assertIsNone(db.kv_get(CURSOR))
        self.send.assert_not_awaited()

    async def test_long_text_is_fully_classified_without_truncating_the_suffix(self):
        text = "Подробный вопрос " * 1000 + "КОНЕЦ_ОСАГО"
        self.telegram.messages = [message(10, text)]
        await press.run_press_monitor(self.cfg, self.send)
        pieces = [item for batch in self.llm.payloads for item in batch]
        self.assertEqual("".join(item["text"] for item in pieces), text)
        self.assertGreater(len(pieces), 1)

    async def test_more_than_150_posts_and_snapshot_arrivals_are_not_lost(self):
        db.kv_set(CURSOR, "1")
        self.telegram.messages = [message(index, f"Запрос {index}") for index in range(2, 163)]
        self.telegram.after_snapshot = lambda: self.telegram.messages.append(message(163, "Новый после снимка"))
        stats = await press.run_press_monitor(self.cfg, self.send)
        self.assertEqual(stats.posts, 161)
        self.assertEqual(db.kv_get(CURSOR), "162")
        stats = await press.run_press_monitor(self.cfg, self.send)
        self.assertEqual(stats.posts, 1)
        self.assertEqual(db.kv_get(CURSOR), "163")

    async def test_first_run_excludes_old_archive_and_preserves_other_sources(self):
        db.kv_set("tg_source_last_id:1470416043", "14629")
        db.kv_set("tg_group_last_id:1678058688", "152040")
        self.telegram.messages = [message(9, "Старый архив", age=timedelta(days=2)), message(10, "Новый запрос")]
        stats = await press.run_press_monitor(self.cfg, self.send)
        self.assertEqual(stats.posts, 1)
        self.assertEqual(db.kv_get("tg_source_last_id:1470416043"), "14629")
        self.assertEqual(db.kv_get("tg_group_last_id:1678058688"), "152040")

    async def test_429_honors_retry_after_and_413_shrinks_batches_without_dropping_requests(self):
        post = linked_post(10, [("Страхование", "https://pressfeed.ru/query/1"), ("Фонды", "https://pressfeed.ru/query/2")])
        self.telegram.messages = [post]
        request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
        self.llm.failures = [RateLimitError("rate limit", response=httpx.Response(429, request=request, headers={"retry-after": "2"}), body={}), APIStatusError("large", response=httpx.Response(413, request=request), body={})]
        self.llm.pick = lambda item: True
        with patch.object(press.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            stats = await press.run_press_monitor(self.cfg, self.send)
        sleep.assert_awaited_once_with(2.0)
        self.assertEqual(stats.sent, 2)
        self.assertEqual(db.kv_get(CURSOR), "10")

    async def test_media_without_caption_is_counted_but_not_invented(self):
        self.telegram.messages = [message(10, "", media=object())]
        stats = await press.run_press_monitor(self.cfg, self.send)
        self.assertEqual(stats.media_only, 1)
        self.assertFalse(self.llm.payloads)
        self.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
