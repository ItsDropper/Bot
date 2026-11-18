import unittest

from config.settings import ConfigurationError, Settings


class SettingsTests(unittest.TestCase):
    def test_loads_optional_secrets_without_breaking_imports(self):
        settings = Settings.from_env({})
        self.assertEqual(settings.port, 3001)
        self.assertIsNone(settings.discord_bot_token)
        self.assertIsNone(settings.cloudflare_worker_url)
        self.assertIsNone(settings.cloudflare_bot_api_token)

    def test_strips_environment_values(self):
        settings = Settings.from_env({
            "PORT": "8080",
            "DISCORD_BOT_TOKEN": " token ",
            "CLOUDFLARE_WORKER_URL": " https://worker.example ",
            "CLOUDFLARE_BOT_API_TOKEN": " secret ",
        })
        self.assertEqual(settings.port, 8080)
        self.assertEqual(settings.discord_bot_token, "token")
        self.assertEqual(settings.cloudflare_worker_url, "https://worker.example")

    def test_rejects_invalid_port_values(self):
        for value in ("zero", "0", "65536", "-1"):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                Settings.from_env({"PORT": value})

    def test_runtime_requirements_are_explicit(self):
        settings = Settings.from_env({})
        with self.assertRaises(ConfigurationError):
            settings.require_discord_token()
        with self.assertRaises(ConfigurationError):
            settings.require_database_credentials()


if __name__ == "__main__":
    unittest.main()
