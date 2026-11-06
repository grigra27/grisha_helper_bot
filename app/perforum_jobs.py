"""Bounded reading of Perforum vacancy cards linked from Telegram announcements."""

import json
import re
from dataclasses import dataclass
from datetime import datetime, time, timezone
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx


MAX_PAGE_BYTES = 1024 * 1024
_JOB_PATH = re.compile(r"/jobs/([1-9][0-9]{0,11})/?\Z")
_VISIBLE_URL = re.compile(r"https?://[^\s<>\"]+", re.I)
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class PerforumError(RuntimeError):
    """A card could not be classified reliably; keep the source cursor for retry."""


@dataclass(frozen=True)
class PerforumJob:
    title: str
    link: str
    company: str
    location: str
    is_spb: bool
    is_remote: bool


def canonical_perforum_url(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or parsed.hostname not in {"perforum.io", "www.perforum.io"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
        ):
            return None
        match = _JOB_PATH.fullmatch(parsed.path)
    except (TypeError, ValueError):
        return None
    return f"https://perforum.io/jobs/{match[1]}" if match else None


def extract_perforum_links(text: str, entities: list[object] | None = None) -> list[str]:
    """Read actual entity targets; no slicing by Telegram's UTF-16 offsets is needed."""
    values = [getattr(entity, "url", "") or "" for entity in entities or []]
    values.extend(value.rstrip(".,);]") for value in _VISIBLE_URL.findall(text or ""))
    urls = []
    for value in values:
        url = canonical_perforum_url(value)
        if url and url not in urls:
            urls.append(url)
    return urls


def _wording(value: str) -> str:
    return " ".join(unescape(value).split())


class _CardHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, set[str]]] = []
        self.jsonld: list[str] = []
        self.script: list[str] | None = None
        self.fields: dict[str, str] = {}
        self.notice_parts: list[str] = []
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    def _in_details(self) -> bool:
        return (
            any(tag == "section" and "section--vacancy" in classes for tag, classes in self.stack)
            and any(tag == "div" and "project-info" in classes for tag, classes in self.stack)
            and any(tag == "table" and "fb-one" in classes for tag, classes in self.stack)
        )

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag not in _VOID_TAGS:
            self.stack.append((tag, set(attrs.get("class", "").split())))
        if tag == "script" and attrs.get("type", "").lower() == "application/ld+json":
            self.script = []
        if tag == "tr" and self._in_details():
            self.row = []
        if tag in {"td", "th"} and self.row is not None:
            self.cell = []
        if tag == "br" and self.cell is not None:
            self.cell.append(" ")

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)
        if self.cell is not None:
            self.cell.append(data)
        if (
            any(tag == "main" for tag, _ in self.stack)
            and any("project-info" in classes for _, classes in self.stack)
            and any("warning-flex" in classes for _, classes in self.stack)
            and not any(tag in {"script", "style"} for tag, _ in self.stack)
        ):
            self.notice_parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.script is not None:
            self.jsonld.append("".join(self.script))
            self.script = None
        if tag in {"td", "th"} and self.cell is not None:
            if self.row is not None:
                self.row.append(_wording("".join(self.cell)))
            self.cell = None
        if tag == "tr" and self.row is not None:
            if len(self.row) == 2:
                key = self.row[0].casefold().rstrip(": ")
                if key in {"location", "arrangement"}:
                    previous = self.fields.get(key)
                    if previous is not None and previous != self.row[1]:
                        raise PerforumError("Perforum card contains ambiguous vacancy details")
                    self.fields[key] = self.row[1]
            self.row = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


def _postings(value):
    if isinstance(value, list):
        for item in value:
            yield from _postings(item)
    elif isinstance(value, dict):
        kind = value.get("@type")
        if kind == "JobPosting" or isinstance(kind, list) and "JobPosting" in kind:
            yield value
        if "@graph" in value:
            yield from _postings(value["@graph"])


def _expired(value, now: datetime) -> bool:
    if not isinstance(value, str) or not value:
        raise PerforumError("Perforum card has no vacancy expiry date")
    try:
        expiry = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if len(value) == 10:
            expiry = datetime.combine(expiry.date(), time.max)
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
    except ValueError:
        raise PerforumError("Perforum card has an invalid vacancy expiry date") from None
    return expiry <= now


def parse_perforum_job(page: str, url: str, *, now: datetime | None = None) -> PerforumJob | None:
    canonical = canonical_perforum_url(url)
    if not canonical:
        raise PerforumError("Invalid Perforum vacancy URL")
    parser = _CardHTML()
    try:
        parser.feed(page)
        if "this vacancy is no longer available" in _wording(" ".join(parser.notice_parts)).casefold():
            return None
        postings = [posting for raw in parser.jsonld for posting in _postings(json.loads(raw))]
    except (ValueError, RecursionError):
        raise PerforumError("Cannot read Perforum vacancy metadata") from None
    identifier = canonical.rsplit("/", 1)[1]
    matching = [
        posting for posting in postings
        if isinstance(posting.get("identifier"), dict)
        and str(posting["identifier"].get("value")) == identifier
    ]
    if len(matching) != 1:
        raise PerforumError("Perforum card does not identify the requested vacancy")
    posting = matching[0]
    if _expired(posting.get("validThrough"), now or datetime.now(timezone.utc)):
        return None
    title = posting.get("title")
    organization = posting.get("hiringOrganization")
    company = organization.get("name", "") if isinstance(organization, dict) else ""
    if not isinstance(title, str) or not title.strip() or not isinstance(company, str):
        raise PerforumError("Perforum card has no valid vacancy title")
    location = parser.fields.get("location", "")
    arrangement = parser.fields.get("arrangement", "")
    if not location:
        places = posting.get("jobLocation", [])
        places = places if isinstance(places, list) else [places]
        localities = []
        for place in places:
            address = place.get("address") if isinstance(place, dict) else None
            locality = address.get("addressLocality") if isinstance(address, dict) else None
            if isinstance(locality, str) and locality.strip():
                localities.append(_wording(locality))
        location = ", ".join(dict.fromkeys(localities))
    types = posting.get("jobLocationType", [])
    types = types if isinstance(types, list) else [types]
    if not arrangement and any(str(value).upper() == "TELECOMMUTE" for value in types):
        # This schema marker means Hybrid or Remote on Perforum. Without its
        # visible counterpart the distinction cannot safely be reconstructed.
        raise PerforumError("Perforum work format could not be confirmed")
    normalized = location.casefold().replace("ё", "е")
    is_spb = bool(re.search(r"\b(?:saint petersburg|st\.? petersburg|санкт[- ]петербург|спб|питер)\b", normalized))
    formats = [item.strip().casefold().replace("ё", "е") for item in arrangement.split(",")]
    # Perforum also labels Hybrid as TELECOMMUTE in JSON-LD. Only the visible
    # Arrangement field reliably distinguishes remote work from Moscow hybrid.
    is_remote = any(value in {"remote", "remote as option", "удаленная работа", "удаленно"} for value in formats)
    return PerforumJob(
        title=_wording(title), link=canonical, company=_wording(company),
        location=location, is_spb=is_spb, is_remote=is_remote,
    )


async def fetch_perforum_job(client: httpx.AsyncClient, url: str) -> PerforumJob | None:
    canonical = canonical_perforum_url(url)
    if not canonical:
        raise PerforumError("Invalid Perforum vacancy URL")
    try:
        # Never follow a link to another host, including redirects from the source site.
        async with client.stream("GET", canonical, follow_redirects=False, timeout=15) as response:
            if response.status_code in {404, 410}:
                return None
            if response.status_code != 200:
                raise PerforumError(f"Perforum vacancy card is unavailable (HTTP {response.status_code})")
            if "text/html" not in response.headers.get("content-type", "").lower():
                raise PerforumError("Perforum returned a non-HTML vacancy card")
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=65536):
                body.extend(chunk)
                if len(body) > MAX_PAGE_BYTES:
                    raise PerforumError("Perforum vacancy card exceeds the size limit")
    except httpx.HTTPError:
        raise PerforumError("Perforum vacancy card could not be downloaded; retry later") from None
    return parse_perforum_job(body.decode("utf-8", "replace"), canonical)
