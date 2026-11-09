from dataclasses import dataclass
import os
import re


def _get_env(name: str, default: str | None = None, required: bool = False) -> str:
    v = os.getenv(name, default)
    if required and (v is None or v == ""):
        raise RuntimeError(f"Missing required env var: {name}")
    return v  # type: ignore[return-value]


def _get_bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or ("1" if default else "0")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    telegram_chat_id: int

    # Telegram user-source (channels -> SPB vacancies)
    telegram_user_enabled: bool
    telegram_user_api_id: int
    telegram_user_api_hash: str
    telegram_user_session: str
    telegram_source_channels: list[str]
    telegram_source_fetch_limit: int
    telegram_vacancy_banned_words: tuple[str, ...]
    telegram_house_chats: list[tuple[str, str]]
    max_chars_per_house_chat_batch: int

    # LLM (Groq/OpenAI-compatible)
    llm_api_key: str
    llm_base_url: str
    llm_model: str

    tz: str
    summary_max_output_tokens: int

    log_level: str
    telegram_group_digest_chat: str = ""
    telegram_group_digest_time: str = "21:00"
    telegram_group_digest_chats: tuple[str, ...] = ()
    telegram_press_channel: str = ""
    telegram_press_poll_minutes: int = 60
    telegram_press_active_hours: str = "09:00-21:00"


def group_digest_sources(cfg) -> tuple[str, ...]:
    """Plural configuration takes precedence; keep the original source compatible."""
    sources = getattr(cfg, "telegram_group_digest_chats", ())
    if not sources:
        sources = (getattr(cfg, "telegram_group_digest_chat", ""),)
    return tuple(dict.fromkeys(source.strip() for source in sources if source.strip()))


def load_config() -> Config:
    # Backward-compatible fallbacks:
    # - If you still have OPENAI_API_KEY/OPENAI_MODEL in .env, it will work.
    llm_api_key = (
        os.getenv("LLM_API_KEY")
        or os.getenv("GROQ_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or ""
    )
    llm_base_url = os.getenv("LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "https://api.groq.com/openai/v1"
    llm_model = os.getenv("LLM_MODEL") or os.getenv("OPENAI_MODEL") or "llama-3.3-70b-versatile"

    telegram_user_enabled = _get_bool("TELEGRAM_USER_ENABLED", False)
    telegram_user_api_id = int(_get_env("TELEGRAM_USER_API_ID", "0"))
    telegram_user_api_hash = _get_env("TELEGRAM_USER_API_HASH", "")
    telegram_user_session = _get_env("TELEGRAM_USER_SESSION", "")
    channels_raw = _get_env("TELEGRAM_SOURCE_CHANNELS", "")
    telegram_source_channels = [x.strip() for x in channels_raw.split(",") if x.strip()]
    banned_words_raw = _get_env("TELEGRAM_VACANCY_BANNED_WORDS", "врач,водитель,агент,терапевт,диспетчер")
    telegram_vacancy_banned_words = tuple(x.strip() for x in banned_words_raw.split(",") if x.strip())
    house_chats_raw = _get_env("TELEGRAM_HOUSE_CHATS", "")
    telegram_house_chats: list[tuple[str, str]] = []
    group_digest_chat = _get_env("TELEGRAM_GROUP_DIGEST_CHAT", "").strip()
    group_digest_chats = tuple(dict.fromkeys(
        item.strip() for item in _get_env("TELEGRAM_GROUP_DIGEST_CHATS", "").split(",") if item.strip()
    ))
    group_digest_time = _get_env("TELEGRAM_GROUP_DIGEST_TIME", "21:00").strip()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", group_digest_time):
        raise RuntimeError("Invalid TELEGRAM_GROUP_DIGEST_TIME; use HH:MM in Moscow time")
    press_channel = _get_env("TELEGRAM_PRESS_CHANNEL", "").strip()
    try:
        press_minutes = int(_get_env("TELEGRAM_PRESS_POLL_MINUTES", "60"))
        if press_minutes not in (5, 10, 15, 20, 30, 60):
            raise ValueError
    except ValueError:
        raise RuntimeError("Invalid TELEGRAM_PRESS_POLL_MINUTES; use 5, 10, 15, 20, 30 or 60") from None
    press_hours = _get_env("TELEGRAM_PRESS_ACTIVE_HOURS", "09:00-21:00").strip()
    clock = r"(?:[01]\d|2[0-3]):[0-5]\d"
    if not re.fullmatch(clock + "-" + clock, press_hours):
        raise RuntimeError("Invalid TELEGRAM_PRESS_ACTIVE_HOURS; use HH:MM-HH:MM in Moscow time")
    for item in house_chats_raw.split(";"):
        part = item.strip()
        if not part:
            continue
        if "=" not in part:
            raise RuntimeError(
                "Invalid TELEGRAM_HOUSE_CHATS format. Use 'Дом 1=@chat_1;Дом 2=@chat_2'"
            )
        house_name, chat_ref = part.split("=", 1)
        house_name = house_name.strip()
        chat_ref = chat_ref.strip()
        if not house_name or not chat_ref:
            raise RuntimeError(
                "Invalid TELEGRAM_HOUSE_CHATS item. Use non-empty 'Имя дома=chat_ref'"
            )
        telegram_house_chats.append((house_name, chat_ref))

    if telegram_user_enabled:
        if not telegram_user_api_id:
            raise RuntimeError("Missing required env var: TELEGRAM_USER_API_ID when TELEGRAM_USER_ENABLED=1")
        if not telegram_user_api_hash:
            raise RuntimeError("Missing required env var: TELEGRAM_USER_API_HASH when TELEGRAM_USER_ENABLED=1")
        if not telegram_user_session:
            raise RuntimeError("Missing required env var: TELEGRAM_USER_SESSION when TELEGRAM_USER_ENABLED=1")
        if not any((telegram_source_channels, telegram_house_chats, group_digest_chat, group_digest_chats, press_channel)):
            raise RuntimeError(
                "Configure TELEGRAM_SOURCE_CHANNELS or TELEGRAM_HOUSE_CHATS or TELEGRAM_GROUP_DIGEST_CHATS or TELEGRAM_PRESS_CHANNEL when TELEGRAM_USER_ENABLED=1"
            )
        if telegram_house_chats and not llm_api_key:
            raise RuntimeError(
                "Missing required env var for house chats: LLM_API_KEY (or GROQ_API_KEY/OPENAI_API_KEY)"
            )
        if (group_digest_chat or group_digest_chats) and not llm_api_key:
            raise RuntimeError(
                "Missing required env var for group digest: LLM_API_KEY (or GROQ_API_KEY/OPENAI_API_KEY)"
            )
        if press_channel and not llm_api_key:
            raise RuntimeError("Missing required env var for press monitor: LLM_API_KEY (or GROQ_API_KEY/OPENAI_API_KEY)")

    return Config(
        telegram_bot_token=_get_env("TELEGRAM_BOT_TOKEN", required=True),
        telegram_chat_id=int(_get_env("TELEGRAM_CHAT_ID", required=True)),

        telegram_user_enabled=telegram_user_enabled,
        telegram_user_api_id=telegram_user_api_id,
        telegram_user_api_hash=telegram_user_api_hash,
        telegram_user_session=telegram_user_session,
        telegram_source_channels=telegram_source_channels,
        telegram_source_fetch_limit=int(_get_env("TELEGRAM_SOURCE_FETCH_LIMIT", "80")),
        telegram_vacancy_banned_words=telegram_vacancy_banned_words,
        telegram_house_chats=telegram_house_chats,
        max_chars_per_house_chat_batch=int(_get_env("MAX_CHARS_PER_HOUSE_CHAT_BATCH", "3000")),

        llm_api_key=llm_api_key,
        llm_base_url=llm_base_url,
        llm_model=llm_model,

        tz=_get_env("TZ", "Europe/Moscow"),
        summary_max_output_tokens=int(_get_env("SUMMARY_MAX_OUTPUT_TOKENS", "220")),

        log_level=_get_env("LOG_LEVEL", "INFO"),
        telegram_group_digest_chat=group_digest_chat,
        telegram_group_digest_time=group_digest_time,
        telegram_group_digest_chats=group_digest_chats,
        telegram_press_channel=press_channel,
        telegram_press_poll_minutes=press_minutes,
        telegram_press_active_hours=press_hours,
    )
