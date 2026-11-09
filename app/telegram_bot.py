import asyncio
import logging
from types import SimpleNamespace
from telegram import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    Update,
)
from telegram.ext import Application, CommandHandler, ContextTypes
from config import Config, group_digest_sources
from db import get_paused, set_paused
from group_digest import run_group_digest
from press_monitor import PressRunStats, run_press_monitor
from telegram_jobs import (
    format_channel_stats,
    format_house_chat_stats,
    run_house_chats_digest,
    run_spb_jobs_digest,
)

logger = logging.getLogger(__name__)


def _is_allowed(update: Update, cfg: Config) -> bool:
    if update.effective_chat is None:
        return False
    return int(update.effective_chat.id) == int(cfg.telegram_chat_id)




def _split_telegram_message(text: str, max_len: int = 3900) -> list[str]:
    """
    Split long text into Telegram-sized chunks without cutting mid-line where possible.
    """
    text = (text or "").strip()
    if len(text) <= max_len:
        return [text] if text else [""]

    lines = text.split("\n")
    chunks: list[str] = []
    buf: list[str] = []
    cur = 0

    for ln in lines:
        add_len = len(ln) + (1 if buf else 0)
        if cur + add_len > max_len and buf:
            chunks.append("\n".join(buf).strip())
            buf = [ln]
            cur = len(ln)
        else:
            if buf:
                cur += 1  # newline
            buf.append(ln)
            cur += len(ln)

    if buf:
        chunks.append("\n".join(buf).strip())

    # Fallback: if a single line is too long, hard-split it
    fixed: list[str] = []
    for ch in chunks:
        if len(ch) <= max_len:
            fixed.append(ch)
        else:
            for i in range(0, len(ch), max_len):
                fixed.append(ch[i:i + max_len])
    return fixed


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg: Config = context.bot_data["cfg"]
    if not _is_allowed(update, cfg):
        return
    paused = get_paused()
    jobs_configured = cfg.telegram_user_enabled and bool(cfg.telegram_source_channels)
    if not cfg.telegram_user_enabled:
        jobs_status = "недоступен: источник Telegram отключён"
    elif not cfg.telegram_source_channels:
        jobs_status = "не настроен: нет каналов вакансий"
    else:
        jobs_status = "на паузе" if paused else "включён"
    jobs_schedule = (
        "- Расписание вакансий: ежедневно в 10:00 UTC (13:00 МСК)\n"
        if jobs_configured else ""
    )
    group_chat = ", ".join(group_digest_sources(cfg))
    if not group_chat:
        group_status = "не настроена"
    elif not cfg.telegram_user_enabled:
        group_status = f"{group_chat}; источник Telegram отключён"
    else:
        group_status = f"{group_chat}; ежедневно в {getattr(cfg, 'telegram_group_digest_time', '21:00')} МСК"
    press_channel = getattr(cfg, "telegram_press_channel", "")
    press_status = "не настроен"
    if press_channel:
        if cfg.telegram_user_enabled:
            press_status = (
                f"{press_channel}; каждые {getattr(cfg, 'telegram_press_poll_minutes', 15)} мин., "
                f"{getattr(cfg, 'telegram_press_active_hours', '09:00-21:00')} МСК"
            )
        else:
            press_status = f"{press_channel}; источник Telegram отключён"
    msg = (
        f"Статус:\n"
        f"- Автосбор вакансий: {jobs_status}\n"
        f"- Источник Telegram: {'включён' if cfg.telegram_user_enabled else 'отключён'}\n"
        f"- Каналов вакансий: {len(cfg.telegram_source_channels)}\n"
        f"{jobs_schedule}"
        f"- Домовых чатов: {len(cfg.telegram_house_chats)}\n"
        f"- Домовые чаты: по команде /house_chats_now\n"
        f"- Модель для сводок: {cfg.llm_model}\n"
        f"- Сводки групп: {group_status}\n"
        f"- Запросы СМИ: {press_status}"
    )
    await update.message.reply_text(msg, disable_web_page_preview=True)


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg: Config = context.bot_data["cfg"]
    if not _is_allowed(update, cfg):
        return
    set_paused(True)
    await update.message.reply_text(
        "Автосбор вакансий поставлен на паузу. Ручные команды вакансий и домовых чатов доступны.",
        disable_web_page_preview=True,
    )


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg: Config = context.bot_data["cfg"]
    if not _is_allowed(update, cfg):
        return
    set_paused(False)
    if not cfg.telegram_user_enabled:
        message = "Пауза снята. Источник Telegram отключён, автосбор вакансий не запланирован."
    elif not cfg.telegram_source_channels:
        message = "Пауза снята. Каналы вакансий не настроены, автосбор вакансий не запланирован."
    else:
        message = "Автосбор вакансий возобновлён: ежедневно в 10:00 UTC (13:00 МСК)."
    await update.message.reply_text(
        message,
        disable_web_page_preview=True,
    )


async def cmd_jobs_spb_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg: Config = context.bot_data["cfg"]
    if not _is_allowed(update, cfg):
        return

    await update.message.reply_text("Ищу вакансии СПб в Telegram-каналах…", disable_web_page_preview=True)
    try:
        text, matched_posts, _channel_stats, commit = await run_spb_jobs_digest(cfg)
        for chunk in _split_telegram_message(text):
            await update.message.reply_text(chunk, disable_web_page_preview=True)
        commit()

        await update.message.reply_text(
            f"Готово. Подходящих постов: {matched_posts}.",
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.exception("jobs_spb_now failed")
        await update.message.reply_text(
            f"Ошибка при сборе вакансий СПб: {e}",
            disable_web_page_preview=True,
        )


async def cmd_house_chats_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg: Config = context.bot_data["cfg"]
    if not _is_allowed(update, cfg):
        return

    await update.message.reply_text("Собираю обновления из домовых чатов…", disable_web_page_preview=True)
    try:
        text, total_messages, chat_stats = await run_house_chats_digest(cfg)
        for chunk in _split_telegram_message(text):
            await update.message.reply_text(chunk, disable_web_page_preview=True)

        await update.message.reply_text(
            f"Готово. Новых сообщений: {total_messages}.\n{format_house_chat_stats(chat_stats)}",
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.exception("house_chats_now failed")
        await update.message.reply_text(
            f"Ошибка при сборе домовых чатов: {e}",
            disable_web_page_preview=True,
        )


async def send_to_owner(app: Application, cfg: Config, text: str, parse_mode: str | None = None) -> None:
    for chunk in _split_telegram_message(text):
        await app.bot.send_message(
            chat_id=cfg.telegram_chat_id,
            text=chunk,
            parse_mode=parse_mode,
            disable_web_page_preview=True,
        )


async def deliver_group_digest(app: Application, cfg: Config) -> int:
    """Deliver groups independently; a failure must not discard other groups' reports."""
    lock = app.bot_data.setdefault("group_digest_lock", asyncio.Lock())
    async with lock:
        count, failed = 0, []
        for source in group_digest_sources(cfg):
            source_cfg = cfg
            if source != getattr(cfg, "telegram_group_digest_chat", ""):
                source_cfg = SimpleNamespace(**{**vars(cfg), "telegram_group_digest_chat": source})
            try:
                text, message_count, commit = await run_group_digest(source_cfg)
                await send_to_owner(app, cfg, text)
                commit()
                count += message_count
            except Exception:
                logger.exception("Group digest failed for %s", source)
                failed.append(source)
        if failed:
            raise RuntimeError("Не удалось отправить сводку: " + ", ".join(failed)) from None
        return count


async def cmd_group_digest_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg: Config = context.bot_data["cfg"]
    if not _is_allowed(update, cfg):
        return
    if not cfg.telegram_user_enabled or not group_digest_sources(cfg):
        await update.message.reply_text("Сводка группы не настроена или источник Telegram отключён.")
        return
    await update.message.reply_text("Собираю сводки обсуждений групп…", disable_web_page_preview=True)
    try:
        count = await deliver_group_digest(context.application, cfg)
        await update.message.reply_text(
            f"Готово. Сообщений группы: {count}.", disable_web_page_preview=True,
        )
    except Exception:
        logger.exception("group_digest_now failed")
        await update.message.reply_text(
            "Не удалось подготовить или отправить сводку группы. Попробуйте ещё раз позже.",
            disable_web_page_preview=True,
        )


async def deliver_press_requests(app: Application, cfg: Config) -> PressRunStats:
    lock = app.bot_data.setdefault("press_monitor_lock", asyncio.Lock())
    async with lock:
        async def send(text):
            await send_to_owner(app, cfg, text)
        return await run_press_monitor(cfg, send)


async def cmd_press_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = context.bot_data["cfg"]
    if not _is_allowed(update, cfg):
        return
    if not cfg.telegram_user_enabled or not getattr(cfg, "telegram_press_channel", ""):
        await update.message.reply_text("Мониторинг запросов СМИ не настроен.")
        return
    try:
        stats = await deliver_press_requests(context.application, cfg)
        if not stats.sent:
            await update.message.reply_text("Новых подходящих запросов СМИ нет.")
    except Exception:
        logger.exception("press_now failed")
        await update.message.reply_text("Не удалось проверить запросы СМИ. Попробуйте позже.")


# Ordered by how often they are used — this is the order of the "Menu" list.
BOT_COMMANDS = [
    BotCommand("jobs_spb_now", "💼 Вакансии СПб из каналов"),
    BotCommand("house_chats_now", "🏠 Сводка домовых чатов"),
    BotCommand("group_digest_now", "💬 Сводки обсуждений групп"),
    BotCommand("press_now", "📰 Запросы СМИ по финансовой тематике"),
    BotCommand("status", "ℹ️ Статус и расписание"),
    BotCommand("pause", "⏸ Пауза автосбора вакансий"),
    BotCommand("resume", "▶️ Возобновить автосбор вакансий"),
    BotCommand("help", "❓ Список команд"),
    BotCommand("start", "▶️ Команды бота"),
]


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg: Config = context.bot_data["cfg"]
    if not _is_allowed(update, cfg):
        return
    lines = ["Вакансии, сводки групп и домовых чатов, финансовые запросы СМИ. Команды доступны через кнопку «Меню»:", ""]
    lines += [f"/{c.command} — {c.description}" for c in BOT_COMMANDS]
    await update.message.reply_text("\n".join(lines), disable_web_page_preview=True)


async def register_bot_commands(app: Application, cfg: Config) -> None:
    # Telegram shows the most specific scope's list, so stale lists left in broader
    # scopes (e.g. set via BotFather for all private chats) would shadow ours.
    # Clear them and publish the menu only to the owner chat.
    await app.bot.delete_my_commands(scope=BotCommandScopeDefault())
    await app.bot.delete_my_commands(scope=BotCommandScopeAllPrivateChats())
    await app.bot.set_my_commands(BOT_COMMANDS, scope=BotCommandScopeChat(cfg.telegram_chat_id))


def build_app(cfg: Config) -> Application:
    application = Application.builder().token(cfg.telegram_bot_token).build()
    application.bot_data["cfg"] = cfg
    application.bot_data["group_digest_lock"] = asyncio.Lock()
    application.bot_data["press_monitor_lock"] = asyncio.Lock()

    application.add_handler(CommandHandler(["start", "help"], cmd_help))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("pause", cmd_pause))
    application.add_handler(CommandHandler("resume", cmd_resume))
    application.add_handler(CommandHandler("jobs_spb_now", cmd_jobs_spb_now))
    application.add_handler(CommandHandler("house_chats_now", cmd_house_chats_now))
    application.add_handler(CommandHandler("group_digest_now", cmd_group_digest_now))
    application.add_handler(CommandHandler("press_now", cmd_press_now))

    return application
