import unittest

from core.database import CloudflareDatabase, MinecraftLinkConflict


class FakeResponse:
    def __init__(self, status, body, headers=None):
        self.status = status
        self.body = body
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self, content_type=None):
        return self.body


class FakeSession:
    closed = False

    def __init__(self, response):
        self.responses = list(response) if isinstance(response, list) else [response]
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


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


    async def test_ticket_assignment_records_staff_and_actor(self):
        client, session = self.make_client(200, {"ticket": {"assigned_to": "423456789012345678"}})
        result = await client.assign_support_ticket(
            "223456789012345678", "423456789012345678",
            "323456789012345678", "2026-11-18T12:00:00",
        )
        self.assertEqual(result["assigned_to"], "423456789012345678")
        self.assertTrue(session.calls[0][1].endswith("/assign"))
        self.assertEqual(session.calls[0][2]["json"]["assigned_by"], "323456789012345678")


    async def test_guild_config_update_and_read(self):
        client, session = self.make_client(200, {"config": {"guild_id": "123456789012345678"}})
        result = await client.update_guild_config(
            "123456789012345678", updated_by="323456789012345678",
            updated_at="2026-11-18T12:00:00", support_role_id="423456789012345678",
        )
        self.assertEqual(result["guild_id"], "123456789012345678")
        self.assertEqual(session.calls[0][0], "PUT")
        self.assertEqual(session.calls[0][2]["json"]["support_role_id"], "423456789012345678")

    async def test_audit_event_is_append_only_post(self):
        client, session = self.make_client(201, {"event_id": 7})
        event_id = await client.append_audit_event(
            guild_id="123456789012345678", actor_id="323456789012345678",
            action="ticket.created", target_id="223456789012345678",
            details={"owner_id": "323456789012345678"}, created_at="2026-11-18T12:00:00",
        )
        self.assertEqual(event_id, 7)
        self.assertEqual(session.calls[0][0], "POST")
        self.assertEqual(session.calls[0][2]["json"]["action"], "ticket.created")

    async def test_audit_listing_is_bounded(self):
        client, session = self.make_client(200, {"events": [{"action": "ticket.created"}]})
        events = await client.list_audit_events("123456789012345678", limit=15)
        self.assertEqual(events[0]["action"], "ticket.created")
        self.assertIn("limit=15", session.calls[0][1])

    async def test_audit_listing_rejects_unbounded_page(self):
        client, session = self.make_client(200, {"events": []})
        with self.assertRaises(ValueError):
            await client.list_audit_events("123456789012345678", limit=26)
        self.assertEqual(session.calls, [])

    async def test_release_announcement_lookup_and_record(self):
        client, session = self.make_client(201, {"announcement": {"release_tag": "v1.3.3"}})
        result = await client.record_release_announcement(
            guild_id="123456789012345678", project="launcher", release_tag="v1.3.3",
            channel_id="223456789012345678", message_id="323456789012345678",
            announced_at="2026-11-18T12:00:00",
        )
        self.assertEqual(result["release_tag"], "v1.3.3")
        self.assertEqual(session.calls[0][0], "POST")
        self.assertEqual(session.calls[0][2]["json"]["project"], "launcher")

    async def test_release_lookup_treats_missing_record_as_none(self):
        client, session = self.make_client(404, {"error": "Release has not been announced"})
        result = await client.get_release_announcement("123456789012345678", "launcher", "v1.3.3")
        self.assertIsNone(result)
        self.assertTrue(session.calls[0][1].endswith("/v1/releases/123456789012345678/launcher/v1.3.3"))



    async def test_add_ticket_note_uses_private_notes_endpoint(self):
        client, session = self.make_client(201, {"event": {"event_id": 9}})
        result = await client.add_ticket_note(
            guild_id="123456789012345678", channel_id="223456789012345678",
            actor_id="323456789012345678", note="Check logs",
            created_at="2026-11-18T12:00:00Z",
        )
        self.assertEqual(result["event_id"], 9)
        self.assertTrue(session.calls[0][1].endswith("/notes"))
        self.assertEqual(session.calls[0][2]["json"]["note"], "Check logs")

    async def test_ticket_priority_is_persisted(self):
        client, session = self.make_client(200, {"ticket": {"priority": "urgent"}})
        result = await client.set_ticket_priority(
            guild_id="123456789012345678", channel_id="223456789012345678",
            actor_id="323456789012345678", priority="urgent",
            created_at="2026-11-18T12:00:00Z",
        )
        self.assertEqual(result["priority"], "urgent")
        self.assertEqual(session.calls[0][0], "PATCH")
        self.assertTrue(session.calls[0][1].endswith("/priority"))

    async def test_ticket_priority_rejects_invalid_value_locally(self):
        client, session = self.make_client(200, {"ticket": {}})
        with self.assertRaises(ValueError):
            await client.set_ticket_priority(
                guild_id="123456789012345678", channel_id="223456789012345678",
                actor_id="323456789012345678", priority="critical",
                created_at="2026-11-18T12:00:00Z",
            )
        self.assertEqual(session.calls, [])

    async def test_ticket_activity_listing_uses_guild_scoped_endpoint(self):
        client, session = self.make_client(200, {"events": [{"action": "internal_note"}]})
        events = await client.list_ticket_activity(
            guild_id="123456789012345678", channel_id="223456789012345678", limit=10,
        )
        self.assertEqual(events[0]["action"], "internal_note")
        self.assertIn("guild_id=123456789012345678", session.calls[0][1])


    async def test_get_retries_transient_worker_errors(self):
        from unittest.mock import AsyncMock, patch

        client, session = self.make_client(503, {"error": "temporarily unavailable"})
        session.responses = [
            FakeResponse(503, {"error": "temporarily unavailable"}),
            FakeResponse(200, {"ok": True}),
        ]
        with patch("core.database.asyncio.sleep", new_callable=AsyncMock) as sleep:
            self.assertTrue(await client.healthcheck())
        self.assertEqual(len(session.calls), 2)
        sleep.assert_awaited_once()

    async def test_write_requests_are_not_retried_without_idempotency_keys(self):
        from core.database import DatabaseError

        client, session = self.make_client(503, {"error": "temporarily unavailable"})
        with self.assertRaises(DatabaseError):
            await client.create_support_ticket(
                ticket_id="1:2", guild_id="123456789012345678",
                channel_id="223456789012345678", owner_id="323456789012345678",
                reason="Launcher crash", created_at="2026-11-18T10:00:00",
            )
        self.assertEqual(len(session.calls), 1)

    async def test_create_moderation_case_persists_pending_record(self):
        case = {"case_id": 41, "action": "warn", "status": "pending"}
        client, session = self.make_client(201, {"case": case})
        result = await client.create_moderation_case(
            guild_id="123456789012345678", target_id="223456789012345678",
            moderator_id="323456789012345678", action="warn", reason="Repeated spam",
            created_at="2026-11-18T12:00:00Z",
        )
        self.assertEqual(result, case)
        self.assertEqual(session.calls[0][0], "POST")
        self.assertTrue(session.calls[0][1].endswith("/v1/moderation/cases"))
        self.assertEqual(session.calls[0][2]["json"]["status"] if "status" in session.calls[0][2]["json"] else "pending", "pending")

    async def test_finalize_moderation_case_records_failure_reason(self):
        case = {"case_id": 41, "status": "failed"}
        client, session = self.make_client(200, {"case": case})
        result = await client.complete_moderation_case(
            41, status="failed", completed_at="2026-11-18T12:01:00Z",
            failure_reason="Forbidden",
        )
        self.assertEqual(result, case)
        self.assertEqual(session.calls[0][0], "PATCH")
        self.assertEqual(session.calls[0][2]["json"]["failure_reason"], "Forbidden")

    async def test_moderation_case_listing_is_guild_and_target_scoped(self):
        rows = [{"case_id": 41, "action": "warn"}]
        client, session = self.make_client(200, {"cases": rows, "total": 1})
        self.assertEqual(
            await client.list_moderation_cases(
                "123456789012345678", target_id="223456789012345678", limit=5
            ),
            (rows, 1),
        )
        self.assertIn("target_id=223456789012345678", session.calls[0][1])

    async def test_moderation_case_listing_rejects_invalid_ids(self):
        client, session = self.make_client(200, {"cases": [], "total": 0})
        with self.assertRaises(ValueError):
            await client.list_moderation_cases("not-a-guild")
        self.assertEqual(session.calls, [])

if __name__ == "__main__":
    unittest.main()
