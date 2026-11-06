import sqlite3
from pathlib import Path
from typing import Optional


DB_PATH = Path("/data/state.db")


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    # Existing tables and rows are preserved, including historical mail data.
    conn = get_conn()
    try:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS kv (
            k TEXT PRIMARY KEY,
            v TEXT NOT NULL
        )
        """)
        conn.commit()
    finally:
        conn.close()


def kv_get(key: str) -> Optional[str]:
    conn = get_conn()
    try:
        row = conn.execute("SELECT v FROM kv WHERE k = ?", (key,)).fetchone()
        return row["v"] if row else None
    finally:
        conn.close()


def kv_set(key: str, value: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def get_paused() -> bool:
    return (kv_get("paused") or "0") == "1"


def set_paused(paused: bool) -> None:
    kv_set("paused", "1" if paused else "0")


def _channel_key(channel_id: str) -> str:
    return f"tg_source_last_id:{channel_id}"


def get_tg_source_last_id(channel_id: str) -> int:
    return int(kv_get(_channel_key(channel_id)) or "0")


def set_tg_source_last_id(channel_id: str, message_id: int) -> None:
    kv_set(_channel_key(channel_id), str(message_id))




def _house_chat_key(chat_id: str) -> str:
    return f"tg_house_last_id:{chat_id}"


def get_tg_house_last_id(chat_id: str) -> int:
    return int(kv_get(_house_chat_key(chat_id)) or "0")


def set_tg_house_last_id(chat_id: str, message_id: int) -> None:
    kv_set(_house_chat_key(chat_id), str(message_id))
