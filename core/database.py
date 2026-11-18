import asyncio
import re
from urllib.parse import quote, urlsplit

import aiohttp


class DatabaseError(RuntimeError):
    """The Cloudflare database service could not complete an operation."""


class MinecraftLinkConflict(DatabaseError):
    """The Minecraft account is already linked to another Discord account."""


class CloudflareDatabase:
    """Async client for the narrowly scoped Vanta Discord D1 Worker API."""

    def __init__(self, base_url: str, api_token: str):
        parsed = urlsplit(base_url)
        local_http = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}
        if parsed.scheme != "https" and not local_http:
            raise ValueError("CLOUDFLARE_WORKER_URL must use HTTPS")
        if not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("CLOUDFLARE_WORKER_URL must be an absolute Worker URL")
        if not api_token or len(api_token) < 32:
            raise ValueError("CLOUDFLARE_BOT_API_TOKEN must contain at least 32 characters")

        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self._session: aiohttp.ClientSession | None = None

    async def start(self):
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=10, connect=4, sock_read=6)
            self._session = aiohttp.ClientSession(timeout=timeout)

    async def close(self):
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _request(self, method: str, path: str, *, payload=None, allow_missing=False):
        if self._session is None or self._session.closed:
            raise DatabaseError("Cloudflare database client is not started")

        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Accept": "application/json",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"

        try:
            async with self._session.request(
                method, f"{self.base_url}{path}", headers=headers, json=payload
            ) as response:
                try:
                    body = await response.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    body = {}

                if response.status == 404 and allow_missing:
                    return None
                if response.status == 409:
                    raise MinecraftLinkConflict("Minecraft username is already linked")
                if response.status < 200 or response.status >= 300:
                    message = body.get("error") if isinstance(body, dict) else None
                    raise DatabaseError(message or f"Cloudflare Worker returned HTTP {response.status}")
                if not isinstance(body, dict):
                    raise DatabaseError("Cloudflare Worker returned an invalid response")
                return body
        except (MinecraftLinkConflict, DatabaseError):
            raise
        except asyncio.TimeoutError as exc:
            raise DatabaseError("Cloudflare Worker request timed out") from exc
        except aiohttp.ClientError as exc:
            raise DatabaseError("Could not reach the Cloudflare Worker") from exc

    async def healthcheck(self):
        result = await self._request("GET", "/v1/health")
        if result.get("ok") is not True:
            raise DatabaseError("Cloudflare D1 health check failed")
        return True

    async def get_minecraft_link(self, discord_id: str):
        result = await self._request(
            "GET", f"/v1/minecraft-links/discord/{quote(str(discord_id), safe='')}",
            allow_missing=True,
        )
        return result.get("link") if result else None

    async def get_minecraft_link_by_username(self, username: str):
        result = await self._request(
            "GET", f"/v1/minecraft-links/username/{quote(username, safe='')}",
            allow_missing=True,
        )
        return result.get("link") if result else None

    async def upsert_minecraft_link(self, discord_id: str, username: str, linked_at: str):
        if not re.fullmatch(r"[0-9]{15,22}", str(discord_id)):
            raise ValueError("Invalid Discord user ID")
        if not re.fullmatch(r"[A-Za-z0-9_]{1,16}", username):
            raise ValueError("Invalid Minecraft username")
        result = await self._request(
            "PUT",
            f"/v1/minecraft-links/discord/{quote(str(discord_id), safe='')}",
            payload={"mc_username": username, "linked_at": linked_at},
        )
        return result["link"]

    async def delete_minecraft_link(self, discord_id: str):
        result = await self._request(
            "DELETE", f"/v1/minecraft-links/discord/{quote(str(discord_id), safe='')}",
            allow_missing=True,
        )
        return result.get("link") if result else None


    async def get_minecraft_link_stats(self):
        result = await self._request("GET", "/v1/minecraft-links/stats")
        return result["stats"]

    async def list_minecraft_links(self, *, limit: int = 10, offset: int = 0):
        if not 1 <= limit <= 25:
            raise ValueError("Minecraft link page size must be between 1 and 25")
        if offset < 0:
            raise ValueError("Minecraft link offset cannot be negative")
        result = await self._request(
            "GET", f"/v1/minecraft-links?limit={limit}&offset={offset}"
        )
        return result["links"], result["total"]


    async def create_support_ticket(self, *, ticket_id: str, guild_id: str, channel_id: str,
                                    owner_id: str, reason: str, created_at: str):
        result = await self._request("POST", "/v1/tickets", payload={
            "ticket_id": ticket_id, "guild_id": guild_id, "channel_id": channel_id,
            "owner_id": owner_id, "reason": reason, "created_at": created_at,
        })
        return result["ticket"]

    async def close_support_ticket(self, channel_id: str, closed_by: str, closed_at: str):
        result = await self._request(
            "PATCH", f"/v1/tickets/channel/{quote(str(channel_id), safe='')}/close",
            payload={"closed_by": str(closed_by), "closed_at": closed_at},
        )
        return result["ticket"]

    async def list_support_tickets(self, guild_id: str, *, owner_id: str | None = None,
                                   status: str = "all", limit: int = 10, offset: int = 0):
        if status not in {"all", "open", "closed"} or not 1 <= limit <= 25 or offset < 0:
            raise ValueError("Invalid support ticket query")
        params = f"guild_id={quote(str(guild_id), safe='')}&status={status}&limit={limit}&offset={offset}"
        if owner_id:
            params += f"&owner_id={quote(str(owner_id), safe='')}"
        result = await self._request("GET", f"/v1/tickets?{params}")
        return result["tickets"], result["total"]

    async def get_support_ticket_stats(self, guild_id: str):
        result = await self._request(
            "GET", f"/v1/tickets/stats?guild_id={quote(str(guild_id), safe='')}"
        )
        return result["stats"]
