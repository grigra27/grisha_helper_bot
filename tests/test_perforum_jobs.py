import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from perforum_jobs import (
    MAX_PAGE_BYTES,
    PerforumError,
    canonical_perforum_url,
    extract_perforum_links,
    fetch_perforum_job,
    parse_perforum_job,
)


NOW = datetime(2026, 11, 6, tzinfo=timezone.utc)


def card(location="Moscow (Москва)", arrangement="Office", **overrides):
    posting = {
        "@type": "JobPosting",
        "identifier": {"value": 123},
        "title": "Аналитик",
        "hiringOrganization": {"name": "Пример банка"},
        "validThrough": "2027-02-04T10:00:00+03:00",
        "jobLocationType": "TELECOMMUTE",
        **overrides,
    }
    return (
        '<script type="application/ld+json">'
        + json.dumps({"@graph": [{"@type": "WebPage"}, posting]})
        + '</script><nav><table><tr><td>Arrangement</td><td>Remote</td></tr></table></nav>'
        + '<section class="section--vacancy"><div class="project-info project-info--progress">'
        + '<table class="fb-one"><tr><td><b>Location&nbsp;&nbsp;</b></td><td>'
        + location
        + '</td></tr><tr><td><b>Arrangement&nbsp;&nbsp;</b></td><td>'
        + arrangement
        + '</td></tr></table></div></section>'
    )


class PerforumParsingTests(unittest.TestCase):
    def test_tracking_and_fragment_links_are_canonicalized(self):
        self.assertEqual(canonical_perforum_url("https://perforum.io/jobs/123?tracking#x"), "https://perforum.io/jobs/123")
        self.assertEqual(canonical_perforum_url("http://www.perforum.io/jobs/123/"), "https://perforum.io/jobs/123")

    def test_only_the_exact_source_host_and_numeric_card_path_are_accepted(self):
        for value in [
            "https://perforum.io.evil.org/jobs/123", "https://evil.org/jobs/123",
            "https://user:secret@perforum.io/jobs/123", "https://perforum.io:443/jobs/123",
            "https://perforum.io/jobs/123/extra", "https://perforum.io/jobs/123evil",
            "https://perforum.io/jobs", "file:///jobs/123", "https://perforum.io/jobs/0",
        ]:
            with self.subTest(value=value):
                self.assertIsNone(canonical_perforum_url(value))
                self.assertEqual(extract_perforum_links(value), [])

    def test_hidden_entity_links_need_no_utf16_title_slicing(self):
        entities = [
            SimpleNamespace(url="https://perforum.io/jobs/123?one", offset=3, length=8),
            SimpleNamespace(url="https://perforum.io/jobs/123?two"),
            SimpleNamespace(url="https://perforum.io/jobs"),
            SimpleNamespace(url="https://perforum.io/news"),
        ]
        self.assertEqual(extract_perforum_links("🧑‍💻 Аналитик", entities), ["https://perforum.io/jobs/123"])

    def test_visible_links_and_entities_deduplicate_by_job_id(self):
        self.assertEqual(
            extract_perforum_links("https://perforum.io/jobs/123?one https://perforum.io/jobs/124.", [SimpleNamespace(url="https://perforum.io/jobs/123?two")]),
            ["https://perforum.io/jobs/123", "https://perforum.io/jobs/124"],
        )

    def test_spb_office_is_selected_from_scoped_location(self):
        job = parse_perforum_job(card(location="Saint Petersburg (Санкт-Петербург), Russia (Россия)"), "https://perforum.io/jobs/123", now=NOW)
        self.assertTrue(job.is_spb)
        self.assertFalse(job.is_remote)
        self.assertEqual(job.company, "Пример банка")

    def test_moscow_hybrid_is_not_remote_despite_telecommute_jsonld(self):
        for arrangement in ["Hybrid", "Office, Hybrid", "Office"]:
            with self.subTest(arrangement=arrangement):
                job = parse_perforum_job(card(arrangement=arrangement), "https://perforum.io/jobs/123", now=NOW)
                self.assertFalse(job.is_spb)
                self.assertFalse(job.is_remote)

    def test_only_explicit_remote_arrangement_is_accepted(self):
        for arrangement in ["Remote", "Remote as option", "Remote, Remote as option, Hybrid", "удалённая работа"]:
            with self.subTest(arrangement=arrangement):
                job = parse_perforum_job(card(arrangement=arrangement), "https://perforum.io/jobs/123", now=NOW)
                self.assertTrue(job.is_remote)

    def test_explicit_remote_does_not_require_a_city(self):
        job = parse_perforum_job(card(location="", arrangement="Remote"), "https://perforum.io/jobs/123", now=NOW)
        self.assertTrue(job.is_remote)
        self.assertFalse(job.is_spb)

    def test_legitimately_unspecified_location_and_format_are_not_invented(self):
        job = parse_perforum_job(card(location="", arrangement="", jobLocationType=None), "https://perforum.io/jobs/123", now=NOW)
        self.assertEqual(job.location, "")
        self.assertFalse(job.is_spb)
        self.assertFalse(job.is_remote)

    def test_schema_locality_is_used_when_visible_location_is_optional(self):
        job = parse_perforum_job(card(location="", jobLocation={"address": {"addressLocality": "Санкт-Петербург"}}), "https://perforum.io/jobs/123", now=NOW)
        self.assertTrue(job.is_spb)

    def test_expired_card_is_excluded_even_when_http200(self):
        self.assertIsNone(parse_perforum_job(card(validThrough="2020-07-14"), "https://perforum.io/jobs/123", now=NOW))

    def test_closed_banner_overrides_stale_future_expiry(self):
        notice = '<div class="warning-flex"><div>This vacancy is no longer available, but you can find similar ones.</div></div>'
        page = '<main>' + card().replace('<table class="fb-one">', notice + '<table class="fb-one">') + '</main>'
        self.assertIsNone(parse_perforum_job(page, "https://perforum.io/jobs/123", now=NOW))

    def test_closed_notice_outside_current_job_does_not_hide_active_card(self):
        page = '<main><aside class="warning-flex">This vacancy is no longer available</aside>' + card() + '</main>'
        self.assertIsNotNone(parse_perforum_job(page, "https://perforum.io/jobs/123", now=NOW))

    def test_old_date_posted_does_not_exclude_a_still_active_job(self):
        self.assertIsNotNone(parse_perforum_job(card(datePosted="2020-01-01"), "https://perforum.io/jobs/123", now=NOW))

    def test_unknown_or_wrong_card_fails_instead_of_silently_advancing(self):
        for page in [
            "<h1>Please sign in</h1>",
            card(identifier={"value": 999}),
            card(validThrough="invalid"),
            card(validThrough=None),
            card(title=None),
            card().replace('class="project-info project-info--progress"', 'class="unrelated"'),
            card().replace("<b>Arrangement&nbsp;&nbsp;</b>", "<b>New field</b>"),
        ]:
            with self.subTest(page=page[:60]):
                with self.assertRaises(PerforumError):
                    parse_perforum_job(page, "https://perforum.io/jobs/123", now=NOW)


class PerforumHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetches_only_canonical_card_without_following_redirects(self):
        requested = []

        def handle(request):
            requested.append(str(request.url))
            return httpx.Response(200, text=card(), headers={"content-type": "text/html"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            job = await fetch_perforum_job(client, "https://perforum.io/jobs/123?tracking")
        self.assertEqual(requested, ["https://perforum.io/jobs/123"])
        self.assertEqual(job.title, "Аналитик")

    async def test_deleted_cards_are_terminal_skips(self):
        for status in [404, 410]:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(status))) as client:
                self.assertIsNone(await fetch_perforum_job(client, "https://perforum.io/jobs/123"))

    async def test_transient_errors_and_redirects_cannot_be_silent_skips(self):
        for status in [301, 302, 429, 500, 503]:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(status, headers={"location": "http://127.0.0.1/"}))) as client:
                with self.assertRaises(PerforumError):
                    await fetch_perforum_job(client, "https://perforum.io/jobs/123")

    async def test_timeout_becomes_safe_retryable_error(self):
        def handle(request):
            raise httpx.ReadTimeout("untrusted server details", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with self.assertRaises(PerforumError) as exc:
                await fetch_perforum_job(client, "https://perforum.io/jobs/123")
        self.assertNotIn("untrusted", str(exc.exception))

    async def test_non_html_and_oversized_pages_are_rejected(self):
        cases = [
            httpx.Response(200, text="{}", headers={"content-type": "application/json"}),
            httpx.Response(200, content=b"x" * (MAX_PAGE_BYTES + 1), headers={"content-type": "text/html"}),
        ]
        for response in cases:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as client:
                with self.assertRaises(PerforumError):
                    await fetch_perforum_job(client, "https://perforum.io/jobs/123")


if __name__ == "__main__":
    unittest.main()
