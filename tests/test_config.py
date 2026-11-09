import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from config import load_config, group_digest_sources


class ConfigTests(unittest.TestCase):
    def load(self, **extra):
        env = {"TELEGRAM_BOT_TOKEN": "123456:test-token", "TELEGRAM_CHAT_ID": "123", **extra}
        with patch.dict(os.environ, env, clear=True):
            return load_config()

    def user_source(self, **extra):
        return self.load(
            TELEGRAM_USER_ENABLED="1",
            TELEGRAM_USER_API_ID="123",
            TELEGRAM_USER_API_HASH="test-hash",
            TELEGRAM_USER_SESSION="test-session",
            **extra,
        )

    def test_starts_without_mail_settings_or_llm_when_sources_disabled(self):
        cfg = self.load()
        self.assertFalse(cfg.telegram_user_enabled)
        self.assertEqual(cfg.llm_api_key, "")
        self.assertFalse(any(name.startswith("imap_") for name in vars(cfg)))

    def test_vacancies_do_not_require_a_model_key(self):
        cfg = self.user_source(TELEGRAM_SOURCE_CHANNELS="@jobs")
        self.assertEqual(cfg.telegram_source_channels, ["@jobs"])
        self.assertEqual(cfg.llm_api_key, "")

    def test_house_chats_still_require_a_model_key(self):
        with self.assertRaisesRegex(RuntimeError, "house chats: LLM_API_KEY"):
            self.user_source(TELEGRAM_HOUSE_CHATS="Дом=@house")

    def test_house_only_source_accepts_groq_fallback(self):
        cfg = self.user_source(TELEGRAM_HOUSE_CHATS="Дом=@house", GROQ_API_KEY="test-key")
        self.assertEqual(cfg.telegram_house_chats, [("Дом", "@house")])
        self.assertEqual(cfg.telegram_source_channels, [])
        self.assertEqual(cfg.llm_api_key, "test-key")

    def test_legacy_mail_configuration_is_ignored(self):
        cfg = self.load(
            IMAP_PORT="obsolete-not-an-integer",
            SCHEDULE_HOURS="obsolete",
            MAX_EMAILS_PER_RUN="obsolete",
            MAX_CHARS_PER_EMAIL="obsolete",
            DIGEST_MAX_OUTPUT_TOKENS="obsolete",
        )
        self.assertFalse(hasattr(cfg, "schedule_hours"))
        self.assertFalse(hasattr(cfg, "max_emails_per_run"))

    def test_house_token_budget_and_llm_aliases_are_preserved(self):
        cfg = self.user_source(
            TELEGRAM_HOUSE_CHATS="Дом=@house",
            OPENAI_API_KEY="legacy-key",
            OPENAI_BASE_URL="https://example.org/v1",
            OPENAI_MODEL="legacy-model",
            SUMMARY_MAX_OUTPUT_TOKENS="400",
        )
        self.assertEqual(cfg.llm_api_key, "legacy-key")
        self.assertEqual(cfg.llm_base_url, "https://example.org/v1")
        self.assertEqual(cfg.llm_model, "legacy-model")
        self.assertEqual(cfg.summary_max_output_tokens, 400)

    def test_enabled_user_requires_at_least_one_source(self):
        with self.assertRaisesRegex(RuntimeError, "TELEGRAM_SOURCE_CHANNELS or TELEGRAM_HOUSE_CHATS"):
            self.user_source()

    def test_group_digest_is_optional_with_compatible_defaults(self):
        cfg = self.load()
        self.assertEqual(cfg.telegram_group_digest_chat, "")
        self.assertEqual(cfg.telegram_group_digest_time, "21:00")

    def test_group_only_source_accepts_groq_key(self):
        cfg = self.user_source(
            TELEGRAM_GROUP_DIGEST_CHAT=" @vakifbank_tr ",
            TELEGRAM_GROUP_DIGEST_TIME="21:00",
            GROQ_API_KEY="test-key",
        )
        self.assertEqual(cfg.telegram_group_digest_chat, "@vakifbank_tr")
        self.assertEqual(cfg.telegram_group_digest_time, "21:00")
        self.assertEqual(cfg.telegram_source_channels, [])
        self.assertEqual(cfg.telegram_house_chats, [])
        self.assertEqual(cfg.llm_api_key, "test-key")

    def test_enabled_group_requires_llm_key_without_house_chats(self):
        with self.assertRaisesRegex(RuntimeError, "group digest: LLM_API_KEY"):
            self.user_source(TELEGRAM_GROUP_DIGEST_CHAT="@vakifbank_tr")

    def test_group_time_rejects_invalid_clock_values(self):
        for value in ("9:00", "24:00", "21:60", "-1:30", "21:00:00", "", "tomorrow"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(RuntimeError, "TELEGRAM_GROUP_DIGEST_TIME"):
                    self.load(TELEGRAM_GROUP_DIGEST_TIME=value)
        for value in ("00:00", "23:59"):
            with self.subTest(value=value):
                self.assertEqual(self.load(TELEGRAM_GROUP_DIGEST_TIME=value).telegram_group_digest_time, value)

    def test_multiple_groups_override_legacy_source_without_duplicates(self):
        cfg = self.user_source(TELEGRAM_GROUP_DIGEST_CHAT="@legacy", TELEGRAM_GROUP_DIGEST_CHATS="@vakifbank_tr, @InteractiveBrokersRUS,@vakifbank_tr", GROQ_API_KEY="test-key")
        self.assertEqual(group_digest_sources(cfg), ("@vakifbank_tr", "@InteractiveBrokersRUS"))
        legacy = self.user_source(TELEGRAM_GROUP_DIGEST_CHAT="@vakifbank_tr", GROQ_API_KEY="test-key")
        self.assertEqual(group_digest_sources(legacy), ("@vakifbank_tr",))

    def test_press_only_source_requires_model_key_and_accepts_window(self):
        with self.assertRaisesRegex(RuntimeError, "press monitor: LLM_API_KEY"):
            self.user_source(TELEGRAM_PRESS_CHANNEL="@Pressfeed_queries")
        cfg = self.user_source(TELEGRAM_PRESS_CHANNEL="@Pressfeed_queries", GROQ_API_KEY="test-key")
        self.assertEqual(cfg.telegram_press_poll_minutes, 15)
        self.assertEqual(cfg.telegram_press_active_hours, "09:00-21:00")
        self.assertEqual(group_digest_sources(cfg), ())
        for key, value in [("TELEGRAM_PRESS_POLL_MINUTES", "0"), ("TELEGRAM_PRESS_POLL_MINUTES", "7"), ("TELEGRAM_PRESS_ACTIVE_HOURS", "9-21"), ("TELEGRAM_PRESS_ACTIVE_HOURS", "09:00-24:00")]:
            with self.subTest(key=key, value=value):
                with self.assertRaisesRegex(RuntimeError, key):
                    self.load(**{key: value})


if __name__ == "__main__":
    unittest.main()
