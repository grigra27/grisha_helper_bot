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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import db
import group_digest


GROUP_ID = 1678058688
CURSOR = f"tg_group_last_id:{GROUP_ID}"
DELIVERED = f"tg_group_last_delivered_at:{GROUP_ID}"
BOOTSTRAP = f"tg_group_bootstrap_since:{GROUP_ID}"


def message(identifier, text, *, age=timedelta(), media=None):
    return SimpleNamespace(
        id=identifier,
        message=text,
        date=datetime.now(timezone.utc) - age,
        media=media,
        action=None,
    )


class FakeTelegram:
    def __init__(self, messages):
        self.messages = messages
        self.iterations = []
        self.after_snapshot = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def is_user_authorized(self):
        return True

    async def get_entity(self, reference):
        return SimpleNamespace(
            id=GROUP_ID, title="VakifBank обсуждение", username="vakifbank_tr",
            megagroup=True, broadcast=False,
        )

    async def get_messages(self, entity, **kwargs):
        latest = max(self.messages, key=lambda item: item.id) if self.messages else None
        if self.after_snapshot is not None:
            callback, self.after_snapshot = self.after_snapshot, None
            callback()
        return [latest] if latest else []

    async def iter_messages(self, entity, **kwargs):
        self.iterations.append(kwargs)
        messages = sorted(self.messages, key=lambda item: item.id, reverse=not kwargs.get("reverse"))
        selected = 0
        for item in messages:
            if item.id <= kwargs.get("min_id", 0):
                continue
            if kwargs.get("max_id") and item.id >= kwargs["max_id"]:
                continue
            if kwargs.get("offset_date") and item.date <= kwargs["offset_date"]:
                continue
            if kwargs.get("limit") is not None and selected >= kwargs["limit"]:
                break
            selected += 1
            yield item


class FakeLLM:
    def __init__(self):
        self.payloads = []
        self.calls = []
        self.returned = []
        self.successful_payloads = []
        self.failures = []
        self.fail_stage_once = {}
        self.finish_reason = "stop"
        self.source_override = None
        self.summary_override = None
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def with_options(self, **kwargs):
        return self

    def close(self):
        pass

    def create(self, **kwargs):
        payload = json.loads(next(
            item["content"] for item in reversed(kwargs["messages"]) if item["role"] == "user"
        ))
        self.calls.append(kwargs)
        self.payloads.append(payload)
        if self.failures:
            raise self.failures.pop(0)
        if payload["stage"] in self.fail_stage_once:
            raise self.fail_stage_once.pop(payload["stage"])
        self.successful_payloads.append(payload)
        if payload["stage"] == "map":
            ids = [item["id"] for item in payload["messages"]]
        else:
            ids = [identifier for topic in payload["topics"] for identifier in topic["source_ids"]]
        ids = list(dict.fromkeys(ids))[:3]
        if self.source_override is not None:
            ids = self.source_override
        topics = [{
            "title": "Обслуживание",
            "summary": self.summary_override or "Участники обсуждали условия обслуживания и обменивались личным опытом.",
            "source_ids": ids,
        }]
        self.returned.append((payload["stage"], topics))
        content = json.dumps({"topics": topics}, ensure_ascii=False)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=content), finish_reason=self.finish_reason,
        )])

    def map_parts(self):
        return [
            item for payload in self.payloads if payload["stage"] == "map"
            for item in payload["messages"]
        ]


class GroupDigestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.database = Path(directory) / "state.db"
        self.enterContext(patch.object(db, "DB_PATH", self.database))
        db.init_db()
        self.cfg = SimpleNamespace(
            telegram_user_enabled=True,
            telegram_user_session="unused-test-session",
            telegram_user_api_id=1,
            telegram_user_api_hash="unused-test-hash",
            telegram_group_digest_chat="@vakifbank_tr",
            llm_api_key="unused-test-key",
            llm_base_url="https://api.groq.com/openai/v1",
            llm_model="test-model",
            summary_max_output_tokens=220,
            tz="Europe/Moscow",
        )
        self.telegram = FakeTelegram([])
        self.llm = FakeLLM()
        self.enterContext(patch.object(group_digest, "TelegramClient", return_value=self.telegram))
        self.enterContext(patch.object(group_digest, "StringSession", return_value=None))
        self.enterContext(patch.object(group_digest, "make_client", return_value=self.llm))

    async def test_incremental_run_reads_all_messages_beyond_150_without_committing_early(self):
        db.kv_set(CURSOR, "10")
        self.telegram.messages = [message(index, f"Обсуждение номер {index}: полный текст.") for index in range(11, 271)]
        text, count, commit = await group_digest.run_group_digest(self.cfg)
        parts = self.llm.map_parts()
        self.assertEqual(count, 260)
        self.assertEqual({part["id"] for part in parts}, set(range(11, 271)))
        for index in range(11, 271):
            self.assertEqual("".join(part["text"] for part in parts if part["id"] == index), f"Обсуждение номер {index}: полный текст.")
        self.assertEqual(db.kv_get(CURSOR), "10")
        self.assertIsNone(db.kv_get(DELIVERED))
        self.assertTrue(text)
        commit()
        self.assertEqual(db.kv_get(CURSOR), "270")
        self.assertIsNotNone(db.kv_get(DELIVERED))

    async def test_first_run_only_reads_the_last_24_hours(self):
        self.telegram.messages = [
            message(1, "Слишком старое сообщение", age=timedelta(hours=25)),
            message(2, "Вчерашнее обсуждение", age=timedelta(hours=23)),
            message(3, "Сегодняшнее обсуждение", age=timedelta(minutes=1)),
        ]
        _, count, commit = await group_digest.run_group_digest(self.cfg)
        self.assertEqual(count, 2)
        self.assertEqual({part["id"] for part in self.llm.map_parts()}, {2, 3})
        self.assertIsNotNone(db.kv_get(BOOTSTRAP))
        self.assertIsNone(db.kv_get(CURSOR))
        commit()
        self.assertEqual(db.kv_get(CURSOR), "3")

    async def test_a_failed_first_run_reuses_its_bootstrap_anchor(self):
        self.telegram.messages = [message(7, "Сохранить обсуждение для повторной попытки")]
        self.llm.failures = [RuntimeError("simulated provider failure")]
        with self.assertRaises(group_digest.GroupDigestError):
            await group_digest.run_group_digest(self.cfg)
        anchor = db.kv_get(BOOTSTRAP)
        self.assertIsNotNone(anchor)
        self.assertIsNone(db.kv_get(CURSOR))
        first_since = self.telegram.iterations[0]["offset_date"]
        _, count, commit = await group_digest.run_group_digest(self.cfg)
        self.assertEqual(count, 1)
        self.assertEqual(db.kv_get(BOOTSTRAP), anchor)
        self.assertEqual(self.telegram.iterations[-1]["offset_date"], first_since)
        self.assertIsNone(db.kv_get(CURSOR))
        commit()
        self.assertEqual(db.kv_get(CURSOR), "7")

    async def test_messages_arriving_after_the_snapshot_wait_for_the_next_run(self):
        db.kv_set(CURSOR, "5")
        self.telegram.messages = [message(6, "Первое"), message(7, "Второе")]
        self.telegram.after_snapshot = lambda: self.telegram.messages.append(message(8, "Следующий запуск"))
        _, count, commit = await group_digest.run_group_digest(self.cfg)
        self.assertEqual(count, 2)
        self.assertEqual({part["id"] for part in self.llm.map_parts()}, {6, 7})
        commit()
        self.assertEqual(db.kv_get(CURSOR), "7")
        self.llm.payloads.clear()
        _, count, commit = await group_digest.run_group_digest(self.cfg)
        self.assertEqual(count, 1)
        self.assertEqual({part["id"] for part in self.llm.map_parts()}, {8})
        commit()
        self.assertEqual(db.kv_get(CURSOR), "8")

    async def test_long_single_message_is_split_without_losing_its_suffix(self):
        db.kv_set(CURSOR, "76")
        original = "Полный_текст_☃_" * 1000 + "КОНЕЦ_ИСХОДНОГО_СООБЩЕНИЯ"
        self.telegram.messages = [message(77, original)]
        await group_digest.run_group_digest(self.cfg)
        parts = self.llm.map_parts()
        self.assertGreater(len(parts), 1)
        self.assertEqual({part["id"] for part in parts}, {77})
        self.assertEqual("".join(part["text"] for part in parts), original)
        self.assertTrue(parts[-1]["text"].endswith("КОНЕЦ_ИСХОДНОГО_СООБЩЕНИЯ"))
        for call in self.llm.calls:
            user = next(item["content"] for item in call["messages"] if item["role"] == "user")
            self.assertLessEqual(len(user), group_digest.MAX_INPUT_CHARS)

    async def test_every_map_batch_summary_reaches_reduction(self):
        db.kv_set(CURSOR, "1")
        self.telegram.messages = [message(index, "Содержательное обсуждение " + "А" * 150) for index in range(2, 32)]
        with (
            patch.object(group_digest, "MAX_INPUT_CHARS", 700),
            patch.object(group_digest, "MAX_TEXT_CHARS", 200),
        ):
            await group_digest.run_group_digest(self.cfg)
        map_outputs = [topics for stage, topics in self.llm.returned if stage == "map"]
        self.assertGreater(len(map_outputs), 6)
        map_sources = {source for topics in map_outputs for topic in topics for source in topic["source_ids"]}
        reduced_sources = {
            source for payload in self.llm.payloads if payload["stage"] == "reduce"
            for topic in payload["topics"] for source in topic["source_ids"]
        }
        self.assertTrue(map_sources <= reduced_sources)

    async def test_provider_failure_does_not_advance_an_existing_cursor(self):
        db.kv_set(CURSOR, "10")
        self.telegram.messages = [message(11, "Новое обсуждение")]
        self.llm.failures = [RuntimeError("simulated provider failure")]
        with self.assertRaises(group_digest.GroupDigestError):
            await group_digest.run_group_digest(self.cfg)
        self.assertEqual(db.kv_get(CURSOR), "10")
        self.assertIsNone(db.kv_get(DELIVERED))

    async def test_truncated_model_output_cannot_be_committed(self):
        db.kv_set(CURSOR, "10")
        self.telegram.messages = [message(11, "Новое обсуждение")]
        self.llm.finish_reason = "length"
        with self.assertRaises(group_digest.GroupDigestError):
            await group_digest.run_group_digest(self.cfg)
        self.assertEqual(db.kv_get(CURSOR), "10")
        self.assertIsNone(db.kv_get(DELIVERED))

    async def test_model_cannot_invent_message_ids_for_source_links(self):
        db.kv_set(CURSOR, "10")
        self.telegram.messages = [message(11, "Новое обсуждение")]
        self.llm.source_override = [999999]
        with self.assertRaises(group_digest.GroupDigestError):
            await group_digest.run_group_digest(self.cfg)
        self.assertEqual(db.kv_get(CURSOR), "10")

    async def test_source_links_are_generated_from_the_group_and_known_message_ids(self):
        db.kv_set(CURSOR, "10")
        self.telegram.messages = [message(11, "Обсуждение комиссии")]
        text, _, _ = await group_digest.run_group_digest(self.cfg)
        self.assertIn("https://t.me/vakifbank_tr/11", text)
        self.assertNotIn("https://t.me/vakifbank_tr/999999", text)

    async def test_model_supplied_urls_are_removed_from_the_summary(self):
        db.kv_set(CURSOR, "10")
        self.telegram.messages = [message(11, "Обсуждение комиссии")]
        self.llm.summary_override = "Участники обсуждали комиссию. https://evil.example/collect"
        text, _, _ = await group_digest.run_group_digest(self.cfg)
        self.assertNotIn("evil.example", text)
        self.assertIn("Участники обсуждали комиссию.", text)
        self.assertIn("https://t.me/vakifbank_tr/11", text)

    async def test_a_summary_empty_after_sanitizing_cannot_be_committed(self):
        db.kv_set(CURSOR, "10")
        self.telegram.messages = [message(11, "Обсуждение комиссии")]
        self.llm.summary_override = "\x00\x01"
        with self.assertRaises(group_digest.GroupDigestError):
            await group_digest.run_group_digest(self.cfg)
        self.assertEqual(db.kv_get(CURSOR), "10")
        self.assertIsNone(db.kv_get(DELIVERED))

    async def test_group_period_uses_moscow_time_even_when_application_timezone_differs(self):
        moment = datetime(2026, 11, 7, 18, tzinfo=timezone.utc)
        item = message(11, "Обсуждение комиссии")
        item.date = moment - timedelta(hours=1)
        self.telegram.messages = [item]
        self.cfg.tz = "UTC"
        with patch.object(group_digest, "_utcnow", return_value=moment):
            text, _, _ = await group_digest.run_group_digest(self.cfg)
        self.assertIn("07.11 21:00", text)
        self.assertIn("МСК", text)
        self.assertNotIn("(UTC)", text)

    async def test_checkpoint_records_snapshot_coverage_not_later_delivery_time(self):
        moment = datetime(2026, 11, 7, 18, tzinfo=timezone.utc)
        item = message(11, "Обсуждение комиссии")
        item.date = moment - timedelta(minutes=1)
        self.telegram.messages = [item]
        clock = [moment]
        with patch.object(group_digest, "_utcnow", side_effect=lambda: clock[0]):
            _, _, commit = await group_digest.run_group_digest(self.cfg)
            clock[0] += timedelta(hours=1)
            commit()
        self.assertEqual(datetime.fromisoformat(db.kv_get(DELIVERED)), moment)

    async def test_media_without_text_is_disclosed_without_inventing_a_summary(self):
        db.kv_set(CURSOR, "10")
        self.telegram.messages = [message(11, "", media=object())]
        text, _, commit = await group_digest.run_group_digest(self.cfg)
        self.assertRegex(text.casefold(), r"медиа|без текст|без подписи")
        self.assertEqual(self.llm.calls, [])
        self.assertEqual(db.kv_get(CURSOR), "10")
        commit()
        self.assertEqual(db.kv_get(CURSOR), "11")

    async def test_sensitive_identifiers_are_masked_before_the_provider_sees_them(self):
        db.kv_set(CURSOR, "10")
        self.telegram.messages = [message(11, (
            "Почта: client@example.org\n"
            "Телефон: +90 532 123 45 67\n"
            "IBAN: TR33 0006 1005 1978 6457 8413 26\n"
            "Карта: 4111 1111 1111 1111\n"
            "код подтверждения: 123456\n"
            "Комиссия 1500 TRY, дата 07.11.2026."
        ))]
        await group_digest.run_group_digest(self.cfg)
        sent = "".join(part["text"] for part in self.llm.map_parts())
        for sensitive in ("client@example.org", "532 123", "TR33", "4111", "123456"):
            self.assertNotIn(sensitive, sent)
        self.assertIn("1500 TRY", sent)
        self.assertIn("07.11.2026", sent)

    async def test_retry_after_429_retries_without_advancing_the_cursor(self):
        db.kv_set(CURSOR, "10")
        self.telegram.messages = [message(11, "Обсуждение комиссии")]
        request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
        response = httpx.Response(429, request=request, headers={"retry-after": "1"})
        self.llm.failures = [RateLimitError("synthetic rate limit", response=response, body=None)]
        with patch.object(group_digest.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            _, _, commit = await group_digest.run_group_digest(self.cfg)
        self.assertEqual(len(self.llm.calls), 2)
        sleep.assert_awaited_once_with(1.0)
        self.assertEqual(db.kv_get(CURSOR), "10")
        self.assertIsNone(db.kv_get(DELIVERED))
        commit()
        self.assertEqual(db.kv_get(CURSOR), "11")

    async def test_413_splits_a_single_large_map_fragment_without_losing_text(self):
        db.kv_set(CURSOR, "10")
        original = "Полное_сообщение_" * 900 + "ОБЯЗАТЕЛЬНЫЙ_ПОСЛЕДНИЙ_ФРАГМЕНТ"
        self.telegram.messages = [message(11, original)]
        request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
        response = httpx.Response(413, request=request)
        self.llm.failures = [APIStatusError("synthetic request size limit", response=response, body=None)]
        _, count, commit = await group_digest.run_group_digest(self.cfg)
        parts = [
            part for payload in self.llm.successful_payloads if payload["stage"] == "map"
            for part in payload["messages"]
        ]
        self.assertEqual(count, 1)
        self.assertEqual("".join(part["text"] for part in parts), original)
        self.assertLess(len(parts[0]["text"]), len(self.llm.payloads[0]["messages"][0]["text"]))
        self.assertEqual(db.kv_get(CURSOR), "10")
        commit()
        self.assertEqual(db.kv_get(CURSOR), "11")

    async def test_413_during_reduction_preserves_every_map_batch(self):
        db.kv_set(CURSOR, "1")
        self.telegram.messages = [message(index, "Обсуждение условий " + "А" * 150) for index in range(2, 32)]
        request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
        response = httpx.Response(413, request=request)
        self.llm.fail_stage_once["reduce"] = APIStatusError("synthetic reduce size limit", response=response, body=None)
        with (
            patch.object(group_digest, "MAX_INPUT_CHARS", 700),
            patch.object(group_digest, "MAX_TEXT_CHARS", 200),
        ):
            _, count, commit = await group_digest.run_group_digest(self.cfg)
        self.assertEqual(count, 30)
        map_sources = {
            source for stage, topics in self.llm.returned if stage == "map"
            for topic in topics for source in topic["source_ids"]
        }
        reduced_sources = {
            source for payload in self.llm.successful_payloads if payload["stage"] == "reduce"
            for topic in payload["topics"] for source in topic["source_ids"]
        }
        self.assertTrue(map_sources <= reduced_sources)
        self.assertEqual(db.kv_get(CURSOR), "1")
        commit()
        self.assertEqual(db.kv_get(CURSOR), "31")

    async def test_checkpoint_and_delivery_time_commit_as_one_transaction(self):
        db.kv_set(CURSOR, "10")
        self.telegram.messages = [message(11, "Обсуждение комиссии")]
        _, _, commit = await group_digest.run_group_digest(self.cfg)
        with sqlite3.connect(self.database) as conn:
            conn.execute(
                "CREATE TRIGGER fail_delivered BEFORE INSERT ON kv "
                f"WHEN NEW.k = '{DELIVERED}' "
                "BEGIN SELECT RAISE(ABORT, 'synthetic checkpoint failure'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            commit()
        self.assertEqual(db.kv_get(CURSOR), "10")
        self.assertIsNone(db.kv_get(DELIVERED))
        with sqlite3.connect(self.database) as conn:
            conn.execute("DROP TRIGGER fail_delivered")
        commit()
        self.assertEqual(db.kv_get(CURSOR), "11")
        self.assertIsNotNone(db.kv_get(DELIVERED))


if __name__ == "__main__":
    unittest.main()
