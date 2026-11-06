import logging
from telegram import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    Update,
)
from telegram.ext import Application, CommandHandler, ContextTypes
from config import Config
from db import get_paused, set_paused
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
    msg = (
        f"Статус:\n"
        f"- Автосбор вакансий: {jobs_status}\n"
        f"- Источник Telegram: {'включён' if cfg.telegram_user_enabled else 'отключён'}\n"
        f"- Каналов вакансий: {len(cfg.telegram_source_channels)}\n"
        f"{jobs_schedule}"
        f"- Домовых чатов: {len(cfg.telegram_house_chats)}\n"
        f"- Домовые чаты: по команде /house_chats_now\n"
        f"- Модель для сводок домовых чатов: {cfg.llm_model}"
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
        text, matched_posts, _channel_stats = await run_spb_jobs_digest(cfg)
        for chunk in _split_telegram_message(text):
            await update.message.reply_text(chunk, disable_web_page_preview=True)

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


# Ordered by how often they are used — this is the order of the "Menu" list.
BOT_COMMANDS = [
    BotCommand("jobs_spb_now", "💼 Вакансии СПб из каналов"),
    BotCommand("house_chats_now", "🏠 Сводка домовых чатов"),
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
    lines = ["Бот вакансий и домовых чатов. Команды также доступны через кнопку «Меню»:", ""]
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

    application.add_handler(CommandHandler(["start", "help"], cmd_help))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("pause", cmd_pause))
    application.add_handler(CommandHandler("resume", cmd_resume))
    application.add_handler(CommandHandler("jobs_spb_now", cmd_jobs_spb_now))
    application.add_handler(CommandHandler("house_chats_now", cmd_house_chats_now))

    return application
