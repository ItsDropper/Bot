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

    async def test_link_stats_endpoint(self):
        client, session = self.make_client(200, {"stats": {"total": 12}})
        self.assertEqual(await client.get_minecraft_link_stats(), {"total": 12})
        self.assertTrue(session.calls[0][1].endswith("/v1/minecraft-links/stats"))

    async def test_list_links_uses_bounded_pagination(self):
        links = [{"discord_id": "123456789012345678", "mc_username": "Dropper", "linked_at": "2026-11-18"}]
        client, session = self.make_client(200, {"links": links, "total": 1})
        self.assertEqual(await client.list_minecraft_links(limit=25, offset=50), (links, 1))
        self.assertTrue(session.calls[0][1].endswith("/v1/minecraft-links?limit=25&offset=50"))

    async def test_list_links_rejects_invalid_page_size(self):
        client, session = self.make_client(200, {"links": [], "total": 0})
        with self.assertRaises(ValueError):
            await client.list_minecraft_links(limit=26, offset=0)
        self.assertEqual(session.calls, [])
    def test_worker_url_must_use_https(self):
        with self.assertRaises(ValueError):
            CloudflareDatabase("http://public.example", "x" * 48)


if __name__ == "__main__":
    unittest.main()


    async def test_create_ticket_posts_persistent_record(self):
        client, session = self.make_client(201, {"ticket": {"ticket_id": "1:2"}})
        result = await client.create_support_ticket(
            ticket_id="1:2", guild_id="123456789012345678",
            channel_id="223456789012345678", owner_id="323456789012345678",
            reason="Launcher crash", created_at="2026-11-18T10:00:00",
        )
        self.assertEqual(result["ticket_id"], "1:2")
        self.assertEqual(session.calls[0][0], "POST")
        self.assertTrue(session.calls[0][1].endswith("/v1/tickets"))

    async def test_close_ticket_records_actor_and_timestamp(self):
        client, session = self.make_client(200, {"ticket": {"status": "closed"}})
        result = await client.close_support_ticket(
            "223456789012345678", "323456789012345678", "2026-11-18T11:00:00"
        )
        self.assertEqual(result["status"], "closed")
        self.assertEqual(session.calls[0][0], "PATCH")
        self.assertEqual(session.calls[0][2]["json"]["closed_by"], "323456789012345678")

    async def test_ticket_listing_encodes_filters(self):
        client, session = self.make_client(200, {"tickets": [], "total": 0})
        tickets, total = await client.list_support_tickets(
            "123456789012345678", owner_id="323456789012345678", status="open"
        )
        self.assertEqual((tickets, total), ([], 0))
        self.assertIn("owner_id=323456789012345678", session.calls[0][1])
