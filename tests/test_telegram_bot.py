import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.append(str(Path(__file__).resolve().parents[1] / "app"))

from telegram import BotCommandScopeAllPrivateChats, BotCommandScopeChat, BotCommandScopeDefault

import telegram_bot


def make_config():
    return SimpleNamespace(
        telegram_bot_token="123456:unit-test-token",
        telegram_chat_id=123456,
        telegram_user_enabled=True,
        telegram_source_channels=["@jobs_one", "@jobs_two"],
        telegram_house_chats=[("Дом 1", "@house_one"), ("Дом 2", "@house_two")],
        llm_model="test-house-model",
    )


def make_update(chat_id=123456):
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )


class TelegramBotCommandTests(unittest.TestCase):
    def test_registered_handlers_and_menu_only_expose_telegram_features(self):
        application = telegram_bot.build_app(make_config())
        commands = {
            command
            for group in application.handlers.values()
            for handler in group
            for command in handler.commands
        }
        expected = {"jobs_spb_now", "house_chats_now", "status", "pause", "resume", "help", "start"}
        self.assertEqual(commands, expected)
        self.assertEqual({command.command for command in telegram_bot.BOT_COMMANDS}, expected)


class TelegramBotOwnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_needs_only_telegram_configuration(self):
        cfg = make_config()
        update = make_update()
        with patch.object(telegram_bot, "get_paused", return_value=False):
            await telegram_bot.cmd_status(update, SimpleNamespace(bot_data={"cfg": cfg}))
        text = update.message.reply_text.await_args.args[0]
        self.assertIn("Автосбор вакансий: включён", text)
        self.assertIn("Каналов вакансий: 2", text)
        self.assertIn("10:00 UTC (13:00 МСК)", text)
        self.assertIn("Домовых чатов: 2", text)
        self.assertIn("/house_chats_now", text)
        self.assertIn("test-house-model", text)
        self.assertNotIn("last_uid", text)
        self.assertNotIn("folder", text)
        self.assertNotIn("1.2", text)

    async def test_help_lists_available_commands_without_removed_mail_commands(self):
        update = make_update()
        await telegram_bot.cmd_help(update, SimpleNamespace(bot_data={"cfg": make_config()}))
        text = update.message.reply_text.await_args.args[0]
        self.assertIn("вакансий и домовых чатов", text)
        self.assertIn("/jobs_spb_now", text)
        self.assertIn("/house_chats_now", text)
        self.assertNotIn("/digest_now", text)
        self.assertNotIn("/stats_csv", text)

    async def test_status_and_resume_do_not_promise_jobs_when_source_is_disabled(self):
        cfg = make_config()
        cfg.telegram_user_enabled = False
        context = SimpleNamespace(bot_data={"cfg": cfg})
        update = make_update()
        with patch.object(telegram_bot, "get_paused", return_value=False):
            await telegram_bot.cmd_status(update, context)
        status = update.message.reply_text.await_args.args[0]
        self.assertIn("источник Telegram отключён", status)
        self.assertNotIn("Расписание вакансий", status)
        self.assertNotIn("13:00", status)
        with patch.object(telegram_bot, "set_paused") as set_paused:
            await telegram_bot.cmd_resume(update, context)
            set_paused.assert_called_once_with(False)
        message = update.message.reply_text.await_args.args[0]
        self.assertIn("Источник Telegram отключён", message)
        self.assertIn("автосбор вакансий не запланирован", message)
        self.assertNotIn("13:00", message)

    async def test_house_only_status_and_resume_do_not_promise_vacancy_jobs(self):
        cfg = make_config()
        cfg.telegram_source_channels = []
        context = SimpleNamespace(bot_data={"cfg": cfg})
        update = make_update()
        with patch.object(telegram_bot, "get_paused", return_value=False):
            await telegram_bot.cmd_status(update, context)
        status = update.message.reply_text.await_args.args[0]
        self.assertIn("нет каналов вакансий", status)
        self.assertIn("/house_chats_now", status)
        self.assertNotIn("Расписание вакансий", status)
        self.assertNotIn("13:00", status)
        with patch.object(telegram_bot, "set_paused") as set_paused:
            await telegram_bot.cmd_resume(update, context)
            set_paused.assert_called_once_with(False)
        message = update.message.reply_text.await_args.args[0]
        self.assertIn("Каналы вакансий не настроены", message)
        self.assertIn("автосбор вакансий не запланирован", message)
        self.assertNotIn("13:00", message)

    async def test_all_remaining_commands_reject_non_owner_and_missing_chat(self):
        context = SimpleNamespace(bot_data={"cfg": make_config()})
        handlers = (
            telegram_bot.cmd_status,
            telegram_bot.cmd_pause,
            telegram_bot.cmd_resume,
            telegram_bot.cmd_help,
            telegram_bot.cmd_jobs_spb_now,
            telegram_bot.cmd_house_chats_now,
        )
        with (
            patch.object(telegram_bot, "get_paused") as get_paused,
            patch.object(telegram_bot, "set_paused") as set_paused,
            patch.object(telegram_bot, "run_spb_jobs_digest", new_callable=AsyncMock) as jobs,
            patch.object(telegram_bot, "run_house_chats_digest", new_callable=AsyncMock) as houses,
        ):
            for handler in handlers:
                for chat in (SimpleNamespace(id=999999), None):
                    with self.subTest(handler=handler.__name__, chat=chat):
                        update = make_update()
                        update.effective_chat = chat
                        await handler(update, context)
                        update.message.reply_text.assert_not_awaited()
            get_paused.assert_not_called()
            set_paused.assert_not_called()
            jobs.assert_not_awaited()
            houses.assert_not_awaited()

    async def test_pause_and_resume_describe_only_the_scheduled_vacancy_job(self):
        context = SimpleNamespace(bot_data={"cfg": make_config()})
        update = make_update()
        with patch.object(telegram_bot, "set_paused") as set_paused:
            await telegram_bot.cmd_pause(update, context)
            set_paused.assert_called_once_with(True)
            text = update.message.reply_text.await_args.args[0]
            self.assertIn("Автосбор вакансий", text)
            self.assertIn("Ручные команды", text)
            self.assertIn("домовых чатов доступны", text)
            set_paused.reset_mock()
            await telegram_bot.cmd_resume(update, context)
            set_paused.assert_called_once_with(False)
            self.assertIn("13:00 МСК", update.message.reply_text.await_args.args[0])

    async def test_manual_vacancy_and_house_commands_still_work_while_paused(self):
        cfg = make_config()
        context = SimpleNamespace(bot_data={"cfg": cfg})
        with (
            patch.object(telegram_bot, "get_paused", return_value=True),
            patch.object(
                telegram_bot, "run_spb_jobs_digest", new_callable=AsyncMock,
                return_value=("Вакансии", 2, [], lambda: None),
            ) as jobs,
            patch.object(
                telegram_bot, "run_house_chats_digest", new_callable=AsyncMock,
                return_value=("Сводка домов", 3, []),
            ) as houses,
        ):
            update = make_update()
            await telegram_bot.cmd_jobs_spb_now(update, context)
            jobs.assert_awaited_once_with(cfg)
            self.assertIn("Вакансии", [call.args[0] for call in update.message.reply_text.await_args_list])
            update = make_update()
            await telegram_bot.cmd_house_chats_now(update, context)
            houses.assert_awaited_once_with(cfg)
            self.assertIn("Сводка домов", [call.args[0] for call in update.message.reply_text.await_args_list])

    async def test_menu_registration_clears_broad_scopes_and_publishes_only_for_owner(self):
        bot = SimpleNamespace(delete_my_commands=AsyncMock(), set_my_commands=AsyncMock())
        cfg = make_config()
        await telegram_bot.register_bot_commands(SimpleNamespace(bot=bot), cfg)
        self.assertEqual(bot.delete_my_commands.await_count, 2)
        scopes = [call.kwargs["scope"] for call in bot.delete_my_commands.await_args_list]
        self.assertIsInstance(scopes[0], BotCommandScopeDefault)
        self.assertIsInstance(scopes[1], BotCommandScopeAllPrivateChats)
        bot.set_my_commands.assert_awaited_once()
        call = bot.set_my_commands.await_args
        self.assertEqual(call.args[0], telegram_bot.BOT_COMMANDS)
        self.assertIsInstance(call.kwargs["scope"], BotCommandScopeChat)
        self.assertEqual(call.kwargs["scope"].chat_id, cfg.telegram_chat_id)

    async def test_vacancy_cursor_is_committed_after_all_chunks_before_acknowledgement(self):
        events = []
        update = make_update()
        text = "A" * 3000 + "\n" + "B" * 3000

        async def reply(message, **kwargs):
            events.append(message)

        update.message.reply_text.side_effect = reply
        commit = Mock(side_effect=lambda: events.append("COMMIT"))
        with patch.object(
            telegram_bot, "run_spb_jobs_digest", new_callable=AsyncMock,
            return_value=(text, 2, [], commit),
        ):
            await telegram_bot.cmd_jobs_spb_now(update, SimpleNamespace(bot_data={"cfg": make_config()}))
        self.assertEqual(events[1:4], ["A" * 3000, "B" * 3000, "COMMIT"])
        self.assertIn("Готово", events[4])
        commit.assert_called_once_with()

    async def test_failed_second_vacancy_chunk_does_not_commit_cursor(self):
        update = make_update()
        update.message.reply_text.side_effect = [None, None, RuntimeError("Telegram send failed"), None]
        commit = Mock()
        with patch.object(
            telegram_bot, "run_spb_jobs_digest", new_callable=AsyncMock,
            return_value=("A" * 3000 + "\n" + "B" * 3000, 2, [], commit),
        ):
            with self.assertLogs(telegram_bot.logger, level="ERROR"):
                await telegram_bot.cmd_jobs_spb_now(update, SimpleNamespace(bot_data={"cfg": make_config()}))
        commit.assert_not_called()
        self.assertIn("Ошибка при сборе", update.message.reply_text.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
