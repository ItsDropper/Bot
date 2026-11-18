"""Validated environment configuration for the Vanta Bot process."""

from dataclasses import dataclass
import os
from collections.abc import Mapping


class ConfigurationError(RuntimeError):
    """Raised when process configuration is invalid."""


@dataclass(frozen=True, slots=True)
class Settings:
    port: int
    discord_bot_token: str | None
    cloudflare_worker_url: str | None
    cloudflare_bot_api_token: str | None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None):
        source = os.environ if environ is None else environ
        try:
            port = int(source.get("PORT", "3001"))
        except (TypeError, ValueError) as exc:
            raise ConfigurationError("PORT must be an integer between 1 and 65535") from exc
        if not 1 <= port <= 65535:
            raise ConfigurationError("PORT must be an integer between 1 and 65535")

        def optional(name):
            value = source.get(name, "").strip()
            return value or None

        return cls(
            port=port,
            discord_bot_token=optional("DISCORD_BOT_TOKEN"),
            cloudflare_worker_url=optional("CLOUDFLARE_WORKER_URL"),
            cloudflare_bot_api_token=optional("CLOUDFLARE_BOT_API_TOKEN"),
        )

    def require_discord_token(self):
        if not self.discord_bot_token:
            raise ConfigurationError("DISCORD_BOT_TOKEN environment variable is required")
        return self.discord_bot_token

    def require_database_credentials(self):
        if not self.cloudflare_worker_url or not self.cloudflare_bot_api_token:
            raise ConfigurationError("CLOUDFLARE_WORKER_URL and CLOUDFLARE_BOT_API_TOKEN are required")
        return self.cloudflare_worker_url, self.cloudflare_bot_api_token
