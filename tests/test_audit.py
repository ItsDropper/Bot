import unittest
from unittest.mock import AsyncMock

from core.audit import AuditService


class AuditServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_record_normalizes_identifiers_and_delegates(self):
        database = type("Database", (), {})()
        database.append_audit_event = AsyncMock(return_value=42)
        service = AuditService(database)

        result = await service.record(
            guild_id=123456789012345678,
            actor_id=323456789012345678,
            action="ticket.created",
            target_id=223456789012345678,
            details={"source": "panel"},
            created_at="2026-11-18T12:00:00",
        )

        self.assertEqual(result, 42)
        database.append_audit_event.assert_awaited_once_with(
            guild_id="123456789012345678",
            actor_id="323456789012345678",
            action="ticket.created",
            target_id="223456789012345678",
            details={"source": "panel"},
            created_at="2026-11-18T12:00:00",
        )

    async def test_record_rejects_empty_or_oversized_action(self):
        database = type("Database", (), {})()
        database.append_audit_event = AsyncMock()
        service = AuditService(database)

        for action in ("", "x" * 65):
            with self.subTest(action_length=len(action)):
                with self.assertRaises(ValueError):
                    await service.record(
                        guild_id="123456789012345678",
                        actor_id="323456789012345678",
                        action=action,
                        created_at="2026-11-18T12:00:00",
                    )
        database.append_audit_event.assert_not_awaited()

    async def test_recent_uses_repository_pagination(self):
        database = type("Database", (), {})()
        database.list_audit_events = AsyncMock(return_value=[{"action": "ticket.created"}])
        service = AuditService(database)

        result = await service.recent("123456789012345678", limit=5)

        self.assertEqual(result, [{"action": "ticket.created"}])
        database.list_audit_events.assert_awaited_once_with("123456789012345678", limit=5)


if __name__ == "__main__":
    unittest.main()
