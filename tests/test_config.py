import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from config import load_config


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


if __name__ == "__main__":
    unittest.main()
