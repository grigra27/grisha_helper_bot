import asyncio
import logging
import os
import re

from config import load_config, Config
from db import init_db, get_paused
from telegram_bot import build_app, send_to_owner, register_bot_commands, deliver_group_digest
from telegram_jobs import format_channel_stats, run_spb_jobs_digest
from scheduler import make_scheduler, add_jobs_digest_job, add_group_digest_job


class RedactTelegramBotTokenFilter(logging.Filter):
    """
    Redacts Telegram bot token from URLs like:
    https://api.telegram.org/bot<token>/method
    """
    _re = re.compile(r"(https://api\.telegram\.org/bot)(\d+:[A-Za-z0-9_-]+)(/)")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True

        if msg:
            redacted = self._re.sub(r"\1[REDACTED]\3", msg)
            if redacted != msg:
                # Overwrite message safely
                record.msg = redacted
                record.args = ()
        return True


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # 1) Redact Telegram token in ANY log line (including from httpx/httpcore libs)
    logging.getLogger().addFilter(RedactTelegramBotTokenFilter())

    # 2) Optional: reduce noisy HTTP client logs (prevents full URL logging)
    # You can change WARNING -> ERROR if you want even less noise.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    # Optional: python-telegram-bot can also be chatty depending on config
    # Keep bot logs, but don't spam with low-level details
    logging.getLogger("telegram").setLevel(logging.INFO)


async def main_async():
    cfg: Config = load_config()
    setup_logging(cfg.log_level)
    init_db()

    # Ensure TZ for APScheduler and logs
    os.environ["TZ"] = cfg.tz

    app = build_app(cfg)

    scheduler = make_scheduler(cfg.tz)

    async def scheduled_spb_jobs_job():
        if get_paused():
            logging.getLogger(__name__).info("Paused; skipping scheduled SPB jobs digest.")
            return
        try:
            text, matched_posts, channel_stats, commit = await run_spb_jobs_digest(cfg)
            await send_to_owner(app, cfg, text)
            commit()
            await send_to_owner(
                app,
                cfg,
                f"Готово. Подходящих постов: {matched_posts}.\n{format_channel_stats(channel_stats)}",
            )
        except Exception as e:
            logging.getLogger(__name__).exception("Scheduled SPB jobs digest failed")
            await send_to_owner(app, cfg, f"Ошибка авто-сбора вакансий СПб: {e}")

    if cfg.telegram_user_enabled and cfg.telegram_source_channels:
        add_jobs_digest_job(scheduler, scheduled_spb_jobs_job)

    async def scheduled_group_digest_job():
        try:
            await deliver_group_digest(app, cfg)
        except Exception:
            logging.getLogger(__name__).exception("Scheduled group digest failed")
            await send_to_owner(
                app, cfg,
                "Не удалось подготовить или отправить сводку группы. Повторим при следующем запуске.",
            )

    if cfg.telegram_user_enabled and cfg.telegram_group_digest_chat:
        add_group_digest_job(scheduler, scheduled_group_digest_job, cfg.telegram_group_digest_time)

    scheduler.start()

    # Start bot (long polling)
    await app.initialize()
    await app.start()
    await register_bot_commands(app, cfg)
    await app.updater.start_polling(drop_pending_updates=True)

    # Keep running
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main_async())
