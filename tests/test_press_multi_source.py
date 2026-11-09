import hashlib
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import db
import press_monitor as press
from press_editorial import EditorialRequest
from press_history import editorial_key
from test_press_monitor import PressMonitorFixture, FakePressTelegram, NOW, linked_post, message

PEERS = {"@Pressfeed_queries": 1110709402, "@partyzan499": 1875460755, "@photodrive": 1149516381}
QUESTION = "Как изменятся тарифы ОСАГО и страховые выплаты?"
BODY = f"Автор @Journalist\nИщем эксперта для комментария.\n{QUESTION}\nОтветы @Journalist"


class MultiTelegram(FakePressTelegram):
    def __init__(self):
        super().__init__([])
        self.by_source = {source: [] for source in PEERS}
        self.unavailable = set()

    async def get_entity(self, reference):
        if reference in self.unavailable:
            raise RuntimeError("private-error-marker")
        self.messages = self.by_source[reference]
        return SimpleNamespace(id=PEERS[reference], title=reference, username=reference.lstrip("@"))


def extracted(question=QUESTION, url=""):
    key = url or editorial_key(question, "@Journalist", "")
    return EditorialRequest(key, question, "@Journalist", "Редакция", "@Journalist", "", None, url)


class MultiSourceTests(PressMonitorFixture):
    def setUp(self):
        super().setUp()
        self.telegram = MultiTelegram()
        self.factory = self.enterContext(patch.object(press, "TelegramClient", return_value=self.telegram))
        self.extract = self.enterContext(patch.object(press, "extract_editorial_requests", new_callable=AsyncMock, return_value=[extracted()]))
        self.cfg.telegram_press_channels = ("@Pressfeed_queries", "@partyzan499", "@photodrive")
        self.llm.pick = lambda item: True

    async def test_one_request_copied_between_raw_channels_is_one_alert(self):
        self.telegram.by_source["@partyzan499"] = [message(10, BODY)]
        self.telegram.by_source["@photodrive"] = [message(20, BODY + "\n@photodrive")]
        stats = await press.run_press_monitor(self.cfg, self.send)
        self.assertEqual(stats.sent, 1)
        self.assertEqual(stats.duplicates, 1)
        self.extract.assert_awaited_once()
        self.send.assert_awaited_once()
        self.assertIn(QUESTION, self.send.await_args.args[0])
        self.assertIn("Ответить: @Journalist", self.send.await_args.args[0])
        self.assertEqual(db.kv_get("tg_press_last_id:1875460755"), "10")
        self.assertEqual(db.kv_get("tg_press_last_id:1149516381"), "20")

    async def test_old_pressfeed_delivery_is_not_repeated_by_a_new_source(self):
        url = "https://pressfeed.ru/query/77"
        legacy = "tg_press_sent:1110709402:" + hashlib.sha256(url.encode()).hexdigest()
        db.kv_set(legacy, "old-delivery")
        self.telegram.by_source["@photodrive"] = [linked_post(20, [(QUESTION, url)], prefix="Автор @Journalist\n" + BODY + "\n")]
        self.extract.return_value = [extracted(url=url)]
        stats = await press.run_press_monitor(self.cfg, self.send)
        self.assertEqual(stats.duplicates, 1)
        self.send.assert_not_awaited()
        self.assertEqual(db.kv_get(legacy), "old-delivery")
        self.assertEqual(db.kv_get("tg_press_sent_global:" + hashlib.sha256(url.encode()).hexdigest()), "old-delivery")

    async def test_pressfeed_query_and_its_repost_share_global_history(self):
        url = "https://pressfeed.ru/query/77"
        self.telegram.by_source["@Pressfeed_queries"] = [linked_post(10, [(QUESTION, url)])]
        self.telegram.by_source["@photodrive"] = [linked_post(20, [(QUESTION, url)], prefix="Автор @Journalist\n" + BODY + "\n")]
        self.extract.return_value = [extracted(url=url)]
        stats = await press.run_press_monitor(self.cfg, self.send)
        self.assertEqual(stats.sent, 1)
        self.assertEqual(stats.duplicates, 1)
        self.send.assert_awaited_once()

    async def test_equal_titles_with_different_query_ids_are_not_merged(self):
        self.telegram.by_source["@Pressfeed_queries"] = [
            linked_post(10, [(QUESTION, "https://pressfeed.ru/query/77")]),
            linked_post(11, [(QUESTION, "https://pressfeed.ru/query/78")]),
        ]
        stats = await press.run_press_monitor(self.cfg, self.send)
        self.assertEqual(stats.sent, 2)
        self.assertEqual(self.send.await_count, 2)

    async def test_unavailable_source_does_not_block_other_sources(self):
        self.telegram.unavailable.add("@partyzan499")
        self.telegram.by_source["@photodrive"] = [message(20, BODY)]
        with self.assertLogs(press.logger, level="ERROR"):
            with self.assertRaises(press.PressMonitorError) as failure:
                await press.run_press_monitor(self.cfg, self.send)
        self.assertNotIn("private-error-marker", str(failure.exception))
        self.send.assert_awaited_once()
        self.assertEqual(db.kv_get("tg_press_last_id:1149516381"), "20")
        self.assertIsNone(db.kv_get("tg_press_last_id:1875460755"))

    async def test_partial_delivery_can_continue_from_another_source_without_repeating_successes(self):
        second = extracted("Какие риски страхования оборудования растут?")
        self.extract.return_value = [extracted(), second]
        self.telegram.by_source["@partyzan499"] = [message(10, BODY)]
        self.telegram.by_source["@photodrive"] = [message(20, BODY + "\n@photodrive")]
        self.send.side_effect = [None, RuntimeError("send-failed"), None]
        with self.assertLogs(press.logger, level="ERROR"):
            with self.assertRaises(press.PressMonitorError):
                await press.run_press_monitor(self.cfg, self.send)
        self.assertEqual(self.send.await_count, 3)
        self.assertIsNone(db.kv_get("tg_press_last_id:1875460755"))
        self.assertEqual(db.kv_get("tg_press_last_id:1149516381"), "20")
        self.send.reset_mock(); self.send.side_effect = None
        stats = await press.run_press_monitor(self.cfg, self.send)
        self.send.assert_not_awaited()
        self.assertEqual(stats.sent, 0)
        self.assertEqual(db.kv_get("tg_press_last_id:1875460755"), "10")

    async def test_editorial_parse_failure_does_not_mark_source_or_post_processed(self):
        self.telegram.by_source["@photodrive"] = [message(20, BODY)]
        self.extract.side_effect = RuntimeError("invalid-extraction")
        with self.assertLogs(press.logger, level="ERROR"):
            with self.assertRaises(press.PressMonitorError):
                await press.run_press_monitor(self.cfg, self.send)
        self.assertIsNone(db.kv_get("tg_press_last_id:1149516381"))
        self.extract.side_effect = None
        stats = await press.run_press_monitor(self.cfg, self.send)
        self.assertEqual(stats.sent, 1)

    async def test_expired_pressfeed_date_is_not_sent(self):
        self.telegram.by_source["@Pressfeed_queries"] = [linked_post(10, [(QUESTION, "https://pressfeed.ru/query/77")])]
        self.enterContext(patch.object(press, "_utcnow", return_value=NOW + timedelta(days=2)))
        self.telegram.by_source["@Pressfeed_queries"][0].date = NOW + timedelta(days=2)
        stats = await press.run_press_monitor(self.cfg, self.send)
        self.assertEqual(stats.rejected, 1)
        self.send.assert_not_awaited()
