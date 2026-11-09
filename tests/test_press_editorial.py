import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from openai import APIStatusError, RateLimitError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import press_editorial as editorial
from press_monitor import TOPIC_PROFILE

PUBLISHED = datetime(2026, 10, 22, 10, tzinfo=timezone.utc)
QUESTION = "Какие страховые продукты и инвестиционные портфели создаются для долголетия?"
TEXT = ("Автор @Journalist\nРедакция делового журнала готовит материал.\n"
        "Приглашаем экспертов дать комментарии.\nДедлайн: 23 октября до позднего вечера.\n\n"
        "7. Вопрос о питании.\n\n8. " + QUESTION + "\n\n9. Вопрос о туризме.\n\nКомментарии присылайте @Journalist")


def candidate(**extra):
    return {"question": QUESTION, "signal": "Приглашаем экспертов дать комментарии.",
        "role": "expert", "personal_story": False, "author": "@Journalist", "outlet": "делового журнала",
        "contact": "@Journalist", "deadline": "23 октября", **extra}


class FakeEditorial:
    def __init__(self):
        self.rows = [candidate()]
        self.payloads, self.failures = [], []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        self.payloads.append(payload)
        if self.failures:
            raise self.failures.pop(0)
        visible = " ".join(payload["fragment"].casefold().split())
        rows = [row for row in self.rows if " ".join(row["question"].casefold().split()) in visible]
        if self.rows and self.rows[0].get("force_bad"):
            rows = self.rows
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
            content=json.dumps({"requests": rows}, ensure_ascii=False)))])


class EditorialTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = FakeEditorial()

    async def extract(self, text=TEXT, *, now=PUBLISHED, links=None):
        return await editorial.extract_editorial_requests(self.client, "test-model", TOPIC_PROFILE,
            text, PUBLISHED, now, links or [])

    async def test_only_selected_numbered_question_is_extracted_with_grounded_metadata(self):
        rows = await self.extract()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].question, "8. " + QUESTION)
        self.assertNotIn("питании", rows[0].question)
        self.assertEqual(rows[0].contact, "@Journalist")
        self.assertIn("23 октября до позднего вечера", rows[0].deadline_raw)

    async def test_expired_request_is_not_resurrected_by_recent_republication(self):
        self.assertEqual(await self.extract(now=datetime(2026, 11, 9, 19, tzinfo=timezone.utc)), [])

    async def test_hero_personal_story_and_offers_are_not_expert_requests(self):
        for role, personal in [("hero", True), ("expert", True), ("offer", False), ("journalist", False)]:
            self.client.rows = [candidate(role=role, personal_story=personal)]
            self.assertEqual(await self.extract(), [])

    async def test_contact_and_identifiable_requester_are_required(self):
        for fields in [{"contact": None}, {"author": None, "outlet": None}]:
            self.client.rows = [candidate(**fields)]
            self.assertEqual(await self.extract(), [])

    async def test_unknown_deadline_is_only_allowed_for_fresh_posts_without_deadline_markers(self):
        text = TEXT.replace("Дедлайн: 23 октября до позднего вечера.\n", "")
        self.client.rows = [candidate(deadline=None)]
        self.assertEqual(len(await self.extract(text, now=PUBLISHED + timedelta(hours=12))), 1)
        self.assertEqual(await self.extract(text, now=PUBLISHED + timedelta(hours=25)), [])
        self.assertEqual(await self.extract(), [])  # A stated deadline cannot be ignored.

    async def test_made_up_quote_or_metadata_cannot_be_delivered(self):
        self.client.rows = [candidate(question="Вымышленный запрос", force_bad=True)]
        with self.assertRaises(editorial.EditorialExtractionError):
            await self.extract()
        self.client.rows = [candidate(outlet="Выдуманное издание")]
        with self.assertRaises(editorial.EditorialExtractionError):
            await self.extract()

    async def test_contact_values_are_masked_for_model_and_restored_for_the_owner(self):
        text = TEXT.replace("Комментарии присылайте @Journalist", "Комментарии присылайте +7 (999) 123-45-67")
        self.client.rows = [candidate(contact="[CONTACT_1]")]
        rows = await self.extract(text)
        self.assertEqual(rows[0].contact, "+7 (999) 123-45-67")
        self.assertNotIn("123-45-67", json.dumps(self.client.payloads))

    async def test_only_known_query_link_can_be_associated_with_a_question(self):
        self.client.rows = [candidate(contact=None, link="https://evil.example/guessed")]
        rows = await self.extract(links=[{"title": QUESTION, "url": "https://pressfeed.ru/query/7"}])
        self.assertEqual(rows[0].url, "https://pressfeed.ru/query/7")
        self.assertEqual(rows[0].key, "https://pressfeed.ru/query/7")

    async def test_long_posts_are_read_to_the_end_in_bounded_fragments(self):
        words = [f"фрагмент_{index:04d}" for index in range(1500)]
        text = " ".join(words) + " КОНЕЦ_ПОСТА"
        self.client.rows = []
        await self.extract(text)
        fragments = [payload["fragment"] for payload in self.client.payloads]
        self.assertTrue(all(word in " ".join(fragments) for word in words))
        self.assertIn("КОНЕЦ_ПОСТА", fragments[-1])
        self.assertTrue(all(len(json.dumps(payload, ensure_ascii=False)) <= editorial.MAX_PAYLOAD_CHARS for payload in self.client.payloads))

    async def test_429_wait_and_413_split_preserve_the_question(self):
        request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
        self.client.failures = [RateLimitError("rate", response=httpx.Response(429, request=request, headers={"retry-after": "2"}), body={}),
            APIStatusError("large", response=httpx.Response(413, request=request), body={})]
        with patch.object(editorial.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            rows = await self.extract()
        sleep.assert_awaited_once_with(2.0)
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
