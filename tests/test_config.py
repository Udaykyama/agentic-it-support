import tempfile
import unittest
from pathlib import Path

from app.config import Settings
from tests.helpers import ENV


class ConfigTests(unittest.TestCase):
    def test_explicit_manual_mode_needs_no_model_secret(self):
        settings = Settings.from_env({**ENV, "AI_ENABLED": "false", "OPENAI_API_KEY": ""})
        self.assertFalse(settings.ai_enabled)

    def test_invalid_configuration_fails_closed(self):
        cases = [
            {"SECRET_KEY": "short"}, {"METRICS_TOKEN": ""},
            {"DATABASE_URL": "sqlite:///tickets.db", "APP_ENV": "development"},
            {"REDIS_URL": "memory://"}, {"AI_ENABLED": "yes"},
            {"AI_ENABLED": "true", "OPENAI_API_KEY": ""},
            {"CONFIDENCE_THRESHOLD": "NaN"}, {"RUNBOOK_SIMILARITY_THRESHOLD": "2"},
            {"JOB_LEASE_SECONDS": "30"}, {"PUBLIC_URL": "https://user:secret@company.example"},
            {"OIDC_CLIENT_ID": ""}, {"OIDC_ISSUER": "not-a-url"},
            {"PUBLIC_URL": "https://company.example/path"},
            {"RATE_LIMIT_PER_MINUTE": "0"},
        ]
        for change in cases:
            with self.subTest(change=change), self.assertRaises(ValueError):
                Settings.from_env({**ENV, **change})

    def test_production_requires_https(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            Settings.from_env({
                **ENV, "APP_ENV": "production",
                "DATABASE_URL": "postgresql+psycopg://app:placeholder@db/app",
            })

    def test_issuer_identifier_is_not_url_normalized(self):
        settings = Settings.from_env({**ENV, "OIDC_ISSUER": "https://identity.example/"})
        self.assertEqual(settings.oidc_issuer, "https://identity.example/")

    def test_secret_files_and_ambiguous_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            filename = Path(folder) / "key"
            filename.write_text("file-secret-not-for-production-0000000\n", encoding="utf-8")
            env = {**ENV, "SECRET_KEY_FILE": str(filename)}
            with self.assertRaisesRegex(ValueError, "not both"):
                Settings.from_env(env)
            env.pop("SECRET_KEY")
            self.assertEqual(Settings.from_env(env).secret_key, "file-secret-not-for-production-0000000")
            filename.unlink()
            with self.assertRaises(FileNotFoundError):
                Settings.from_env(env)
