import asyncio
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

import httpx

from telethon import TelegramClient
from telethon.sessions import StringSession

from city_extract import (
    VacancyItem,
    _is_banned_title,
    extract_inline_hh_links_from_entities,
    parse_remote_vacancies,
    parse_spb_vacancies,
)
from config import Config
from db import (
    get_tg_house_last_id,
    get_tg_source_last_id,
    set_tg_house_last_id,
    set_tg_source_last_id,
)
from llm import make_client, summarize_house_chat_messages
from perforum_jobs import extract_perforum_links, fetch_perforum_job

logger = logging.getLogger(__name__)

MAX_TELEGRAM_SOURCE_FETCH_LIMIT = 300


def _effective_source_fetch_limit(raw_limit: int, default_limit: int = 80) -> int:
    try:
        parsed_limit = int(raw_limit)
    except (TypeError, ValueError):
        return default_limit

    if parsed_limit <= 0:
        return default_limit

    return min(parsed_limit, MAX_TELEGRAM_SOURCE_FETCH_LIMIT)


@dataclass(frozen=True)
class ChannelRunStats:
    channel_ref: str
    channel_title: str
    fetched_posts: int
    detected_vacancies: int
    selected_vacancies: int


@dataclass(frozen=True)
class HouseChatRunStats:
    house_name: str
    chat_ref: str
    chat_title: str
    fetched_messages: int


def format_channel_stats(channel_stats: list[ChannelRunStats]) -> str:
    if not channel_stats:
        return "Каналы: нет данных."

    lines = ["Статистика по каналам:"]
    for st in channel_stats:
        lines.append(
            f"- {st.channel_title} ({st.channel_ref}): постов просмотрено {st.fetched_posts}, "
            f"вакансий отсмотрено {st.detected_vacancies}, выбрано {st.selected_vacancies}"
        )
    return "\n".join(lines)


def format_house_chat_stats(chat_stats: list[HouseChatRunStats]) -> str:
    if not chat_stats:
        return "Чаты домов: нет данных."

    lines = ["Статистика по чатам домов:"]
    for st in chat_stats:
        lines.append(
            f"- {st.house_name} ({st.chat_title}, {st.chat_ref}): сообщений просмотрено {st.fetched_messages}"
        )
    return "\n".join(lines)


def _fmt_dt(dt: datetime | None) -> str:
    if not dt:
        return "unknown-date"
    return dt.strftime("%Y-%m-%d %H:%M")


def _cap_messages_blob(rendered_messages: list[str], max_chars: int) -> str:
    """
    Keeps as many of the most recent messages as fit in max_chars
    (oldest ones are dropped first), preserving chronological order
    in the output. Without this cap a chat with a large backlog can
    blow past the LLM provider's per-request token limit (e.g. Groq's
    413 "Request too large" on tokens-per-minute).
    """
    kept: list[str] = []
    total = 0
    for msg in reversed(rendered_messages):
        length = len(msg) + (1 if kept else 0)
        if total + length > max_chars and kept:
            break
        kept.append(msg)
        total += length
    kept.reverse()
    return "\n".join(kept)


async def run_spb_jobs_digest(cfg: Config) -> tuple[str, int, list[ChannelRunStats], Callable[[], None]]:
    if not cfg.telegram_user_enabled:
        return "Источник Telegram-каналов отключён (TELEGRAM_USER_ENABLED=0).", 0, [], lambda: None

    client = TelegramClient(StringSession(cfg.telegram_user_session), cfg.telegram_user_api_id, cfg.telegram_user_api_hash)
    fetch_limit = _effective_source_fetch_limit(cfg.telegram_source_fetch_limit)

    lines: list[str] = ["Вакансии Санкт-Петербурга из Telegram-каналов:"]
    remote_lines: list[str] = []
    matched_posts = 0
    channel_stats: list[ChannelRunStats] = []
    cursors: dict[str, int] = {}
    page_cache: dict[str, object] = {}
    seen_perforum: set[str] = set()
    http_client = None
    page_slots = asyncio.Semaphore(3)

    async def load_page(url):
        async with page_slots:
            return await fetch_perforum_job(http_client, url)

    async with client, AsyncExitStack() as stack:
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram user session is not authorized. Recreate TELEGRAM_USER_SESSION.")

        for channel_ref in cfg.telegram_source_channels:
            entity = await client.get_entity(channel_ref)
            channel_id = str(entity.id)
            channel_title = getattr(entity, "title", None) or channel_ref
            last_id = get_tg_source_last_id(channel_id)

            msgs = await client.get_messages(
                entity, limit=fetch_limit, min_id=last_id, reverse=bool(last_id)
            )
            msgs = sorted((msg for msg in msgs if msg and getattr(msg, "id", None)), key=lambda msg: msg.id)
            links_by_post = {
                msg.id: extract_perforum_links(msg.message or "", getattr(msg, "entities", None))
                for msg in msgs
            }
            new_links = list(dict.fromkeys(
                url for links in links_by_post.values() for url in links if url not in page_cache
            ))
            if new_links:
                if http_client is None:
                    http_client = await stack.enter_async_context(httpx.AsyncClient(
                        timeout=15,
                        limits=httpx.Limits(max_connections=3, max_keepalive_connections=3),
                    ))
                fetched = await asyncio.gather(*(load_page(url) for url in new_links), return_exceptions=True)
                for url, job in zip(new_links, fetched):
                    if isinstance(job, BaseException):
                        raise job
                    page_cache[url] = job

            max_seen = last_id
            new_hits = 0
            detected_vacancies = 0
            selected_vacancies = 0

            for msg in msgs:
                if not msg or not getattr(msg, "id", None):
                    continue
                if msg.id > max_seen:
                    max_seen = msg.id

                text = (msg.message or "").strip()
                if not text:
                    continue

                if links_by_post[msg.id]:
                    spb_vacancies, remote_vacancies = [], []
                    for url in links_by_post[msg.id]:
                        if url in seen_perforum:
                            continue
                        seen_perforum.add(url)
                        detected_vacancies += 1
                        job = page_cache[url]
                        if job is None or _is_banned_title(job.title, cfg.telegram_vacancy_banned_words):
                            continue
                        item = VacancyItem(job.title, job.link, job.company)
                        if job.is_remote:
                            remote_vacancies.append(item)
                        elif job.is_spb:
                            spb_vacancies.append(item)
                else:
                    inline_title_links = extract_inline_hh_links_from_entities(text, getattr(msg, "entities", None))
                    parse_result = parse_spb_vacancies(
                        text,
                        banned_keywords=cfg.telegram_vacancy_banned_words,
                        inline_title_links=inline_title_links,
                    )
                    remote_result = parse_remote_vacancies(
                        text,
                        banned_keywords=cfg.telegram_vacancy_banned_words,
                        inline_title_links=inline_title_links,
                    )
                    detected_vacancies += parse_result.detected_items
                    detected_vacancies += remote_result.detected_items
                    spb_vacancies = parse_result.selected_items
                    remote_vacancies = remote_result.selected_items
                if not spb_vacancies and not remote_vacancies:
                    continue

                selected_vacancies += len(spb_vacancies) + len(remote_vacancies)
                new_hits += 1
                company = (spb_vacancies or remote_vacancies)[0].company if (spb_vacancies or remote_vacancies) else ""
                header = f"\nКанал: {channel_title} | пост #{msg.id} | {_fmt_dt(msg.date)}"
                if company:
                    header += f" | Компания: {company}"

                if spb_vacancies:
                    matched_posts += 1
                    lines.append(header)
                    for idx, item in enumerate(spb_vacancies, start=1):
                        lines.append(f"{idx}. {item.title}")
                        lines.append(f"   {item.link}")

                if remote_vacancies:
                    if not spb_vacancies:
                        matched_posts += 1
                    remote_lines.append(header)
                    for idx, item in enumerate(remote_vacancies, start=1):
                        remote_lines.append(f"{idx}. {item.title}")
                        remote_lines.append(f"   {item.link}")

            cursors[channel_id] = max_seen
            channel_stats.append(
                ChannelRunStats(
                    channel_ref=channel_ref,
                    channel_title=channel_title,
                    fetched_posts=len(msgs),
                    detected_vacancies=detected_vacancies,
                    selected_vacancies=selected_vacancies,
                )
            )
            logger.info(
                "Channel %s processed: fetched_posts=%s, matched_posts=%s, detected_vacancies=%s, selected_vacancies=%s, last_id=%s",
                channel_ref,
                len(msgs),
                new_hits,
                detected_vacancies,
                selected_vacancies,
                max_seen,
            )

    stats_block = format_channel_stats(channel_stats)

    committed = False

    def commit():
        nonlocal committed
        if not committed:
            for channel_id, message_id in cursors.items():
                set_tg_source_last_id(channel_id, max(message_id, get_tg_source_last_id(channel_id)))
            committed = True

    if matched_posts == 0:
        return f"В новых постах по выбранным каналам вакансий СПб и удалённой работы не найдено.\n\n{stats_block}", 0, channel_stats, commit

    if remote_lines:
        lines.append("\nудаленная работа:")
        lines.extend(remote_lines)

    return "\n".join(lines), matched_posts, channel_stats, commit


async def run_house_chats_digest(cfg: Config) -> tuple[str, int, list[HouseChatRunStats]]:
    if not cfg.telegram_user_enabled:
        return "Источник Telegram-чатов отключён (TELEGRAM_USER_ENABLED=0).", 0, []
    if not cfg.telegram_house_chats:
        return "Не настроены домовые чаты (TELEGRAM_HOUSE_CHATS пуст).", 0, []

    llm_client = make_client(cfg.llm_api_key, cfg.llm_base_url)
    tg_client = TelegramClient(StringSession(cfg.telegram_user_session), cfg.telegram_user_api_id, cfg.telegram_user_api_hash)

    lines: list[str] = ["Отчёт по домовым чатам:"]
    total_new_messages = 0
    chat_stats: list[HouseChatRunStats] = []

    async with tg_client:
        if not await tg_client.is_user_authorized():
            raise RuntimeError("Telegram user session is not authorized. Recreate TELEGRAM_USER_SESSION.")

        for house_name, chat_ref in cfg.telegram_house_chats:
            try:
                entity = await tg_client.get_entity(chat_ref)
                chat_id = str(entity.id)
                chat_title = getattr(entity, "title", None) or house_name
                last_id = get_tg_house_last_id(chat_id)

                msgs = await tg_client.get_messages(entity, limit=cfg.telegram_source_fetch_limit, min_id=last_id)

                max_seen = last_id
                rendered_messages: list[str] = []

                for msg in reversed(msgs):
                    if not msg or not getattr(msg, "id", None):
                        continue
                    if msg.id > max_seen:
                        max_seen = msg.id

                    text = (msg.message or "").strip()
                    if not text:
                        continue

                    rendered_messages.append(f"[{_fmt_dt(msg.date)}] {text}")

                if rendered_messages:
                    messages_blob = _cap_messages_blob(rendered_messages, cfg.max_chars_per_house_chat_batch)
                    summary = summarize_house_chat_messages(
                        client=llm_client,
                        model=cfg.llm_model,
                        house_name=house_name,
                        messages_blob=messages_blob,
                        max_output_tokens=cfg.summary_max_output_tokens,
                    )
                else:
                    summary = "новых обсуждений нет"

                # Only advance last_id once the summary actually succeeded, so a
                # failed chat gets retried (not silently skipped) next run.
                set_tg_house_last_id(chat_id, max_seen)
                total_new_messages += len(msgs)
                chat_stats.append(
                    HouseChatRunStats(
                        house_name=house_name,
                        chat_ref=chat_ref,
                        chat_title=chat_title,
                        fetched_messages=len(msgs),
                    )
                )
                lines.append(f"- {house_name}: {summary}")
                logger.info(
                    "House chat %s processed: fetched_messages=%s, last_id=%s",
                    chat_ref,
                    len(msgs),
                    max_seen,
                )
            except Exception:
                # One bad chat (LLM error, fetch failure, etc.) must not abort
                # the remaining chats in this run.
                logger.exception("House chat %s failed", chat_ref)
                lines.append(f"- {house_name}: ошибка при сборе сводки (см. логи), попробуем ещё раз в следующий запуск")

    return "\n".join(lines), total_new_messages, chat_stats
