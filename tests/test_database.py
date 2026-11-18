import unittest

from core.database import CloudflareDatabase, MinecraftLinkConflict


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self.body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self, content_type=None):
        return self.body


class FakeSession:
    closed = False

    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response


class CloudflareDatabaseTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self, status, body):
        client = CloudflareDatabase("https://vanta-discord.example.workers.dev", "x" * 48)
        session = FakeSession(FakeResponse(status, body))
        client._session = session
        return client, session

    async def test_missing_link_returns_none(self):
        client, session = self.make_client(404, {"error": "Link not found"})
        self.assertIsNone(await client.get_minecraft_link("123456789012345678"))
        self.assertEqual(
            session.calls[0][1],
            "https://vanta-discord.example.workers.dev/v1/minecraft-links/discord/123456789012345678",
        )

    async def test_get_link_returns_record(self):
        link = {"discord_id": "123456789012345678", "mc_username": "Dropper", "linked_at": "2026-11-18T10:00:00"}
        client, _ = self.make_client(200, {"link": link})
        self.assertEqual(await client.get_minecraft_link("123456789012345678"), link)

    async def test_duplicate_username_raises_conflict(self):
        client, _ = self.make_client(409, {"error": "Already linked"})
        with self.assertRaises(MinecraftLinkConflict):
            await client.upsert_minecraft_link("123456789012345678", "Dropper", "2026-11-18T10:00:00")

    async def test_invalid_username_is_rejected_locally(self):
        client, session = self.make_client(200, {"link": {}})
        with self.assertRaises(ValueError):
            await client.upsert_minecraft_link("123456789012345678", "invalid name", "2026-11-18T10:00:00")
        self.assertEqual(session.calls, [])

    def test_worker_url_must_use_https(self):
        with self.assertRaises(ValueError):
            CloudflareDatabase("http://public.example", "x" * 48)


if __name__ == "__main__":
    unittest.main()
