import unittest
from unittest.mock import AsyncMock

from core.moderation import ModerationService


class ModerationServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.database = type("Database", (), {})()
        self.database.create_moderation_case = AsyncMock(return_value={"case_id": 7, "status": "pending"})
        self.database.complete_moderation_case = AsyncMock(return_value={"case_id": 7, "status": "applied"})
        self.database.list_moderation_cases = AsyncMock(return_value=([{"case_id": 7}], 1))
        self.service = ModerationService(self.database)

    async def test_create_case_normalizes_ids_and_reason(self):
        result = await self.service.create_case(
            guild_id=123456789012345678, target_id=223456789012345678,
            moderator_id=323456789012345678, action="warn", reason="  spam  ",
            created_at="2026-11-18T12:00:00Z",
        )
        self.assertEqual(result["case_id"], 7)
        self.database.create_moderation_case.assert_awaited_once()
        self.assertEqual(self.database.create_moderation_case.await_args.kwargs["reason"], "spam")

    async def test_create_case_rejects_invalid_actor_and_action(self):
        with self.assertRaises(ValueError):
            await self.service.create_case(
                guild_id="invalid", target_id="223456789012345678",
                moderator_id="323456789012345678", action="warn", reason="reason",
            )
        with self.assertRaises(ValueError):
            await self.service.create_case(
                guild_id="123456789012345678", target_id="223456789012345678",
                moderator_id="323456789012345678", action="delete", reason="reason",
            )
        self.database.create_moderation_case.assert_not_awaited()

    async def test_create_case_rejects_empty_or_oversized_reason(self):
        for reason in (" ", "x" * 513):
            with self.subTest(length=len(reason)), self.assertRaises(ValueError):
                await self.service.create_case(
                    guild_id="123456789012345678", target_id="223456789012345678",
                    moderator_id="323456789012345678", action="warn", reason=reason,
                )
        self.database.create_moderation_case.assert_not_awaited()

    async def test_complete_case_rejects_nonterminal_state(self):
        with self.assertRaises(ValueError):
            await self.service.complete_case(7, status="pending")
        self.database.complete_moderation_case.assert_not_awaited()

    async def test_list_cases_enforces_page_bounds(self):
        self.assertEqual(
            await self.service.list_cases("123456789012345678", limit=5),
            ([{"case_id": 7}], 1),
        )
        with self.assertRaises(ValueError):
            await self.service.list_cases("123456789012345678", limit=100)
        self.database.list_moderation_cases.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
