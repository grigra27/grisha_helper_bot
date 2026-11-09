"""Global delivery history shared by every press source, including legacy keys."""

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone

from db import get_conn, kv_get, kv_set


def normalized_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(text.split())


def normalized_post(text: str) -> str:
    text = re.sub(r"@(?:photodrive|partyzan499)\b", "", text, flags=re.I)
    text = re.sub(r"^\s*(?:Автор\s+@\w+\s*)+", "", text, flags=re.I)
    return normalized_text(text)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def migrate_legacy_history() -> None:
    """A previously sent Pressfeed URL stays sent when copied by another source."""
    conn = get_conn()
    try:
        with conn:
            rows = conn.execute("SELECT k,v FROM kv WHERE k LIKE 'tg_press_sent:%'").fetchall()
            for row in rows:
                signature = row["k"].rsplit(":", 1)[-1]
                if re.fullmatch(r"[a-f0-9]{64}", signature):
                    conn.execute("INSERT OR IGNORE INTO kv(k,v) VALUES(?,?)", ("tg_press_sent_global:" + signature, row["v"]))
    finally:
        conn.close()


def was_sent(request_key: str) -> bool:
    return kv_get("tg_press_sent_global:" + digest(request_key)) is not None


def remember_sent(source_id: int, request_key: str) -> None:
    conn = get_conn()
    try:
        with conn:
            value = datetime.now(timezone.utc).isoformat()
            signature = digest(request_key)
            for key in (f"tg_press_sent:{source_id}:{signature}", "tg_press_sent_global:" + signature):
                conn.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))
    finally:
        conn.close()


def post_cache_key(text: str, request_urls: list[str]) -> str | None:
    if not text.strip():
        return None
    # Hidden URLs are part of identity: equal titles with different query IDs
    # must not be collapsed into one request.
    content = json.dumps([normalized_post(text), sorted(set(request_urls))], ensure_ascii=False)
    return "tg_press_processed_post:" + digest(content)


def post_was_processed(key: str | None) -> bool:
    return bool(key and kv_get(key) is not None)


def remember_post(key: str | None) -> None:
    if key:
        kv_set(key, datetime.now(timezone.utc).isoformat())


def editorial_key(question: str, contact: str, deadline: str) -> str:
    question = re.sub(r"^\s*(?:\d+[.)]|[-•])\s*", "", question)
    contact = normalized_text(contact)
    handles = re.findall(r"@[a-zA-Z][a-zA-Z0-9_]{4,31}\b", contact)
    emails = re.findall(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", contact)
    if handles or emails:
        contact = "|".join(sorted(set(handles + emails)))
    else:
        contact = re.sub(r"\D", "", contact) or contact
    content = [normalized_post(question), contact, deadline]
    return "editorial:" + json.dumps(content, ensure_ascii=False, separators=(",", ":"))
