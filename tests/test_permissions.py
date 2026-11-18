import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.permissions import PermissionService


class PermissionServiceTests(unittest.IsolatedAsyncioTestCase):
    def make_member(self, *, administrator=False, roles=()):
        return SimpleNamespace(
            guild_permissions=SimpleNamespace(administrator=administrator),
            roles=list(roles),
        )

    def make_guild(self, role):
        return SimpleNamespace(id=123456789012345678, get_role=lambda role_id: role if role_id == role.id else None)

    async def test_administrator_bypasses_role_lookup(self):
        database = SimpleNamespace(get_guild_config=AsyncMock())
        service = PermissionService(lambda: database, 99)
        member = self.make_member(administrator=True)
        self.assertTrue(await service.is_support_staff(member, self.make_guild(None)))
        database.get_guild_config.assert_not_awaited()

    async def test_uses_configured_support_role(self):
        role = SimpleNamespace(id=44)
        database = SimpleNamespace(get_guild_config=AsyncMock(return_value={"support_role_id": "44"}))
        service = PermissionService(lambda: database, 99)
        self.assertTrue(await service.is_support_staff(self.make_member(roles=[role]), self.make_guild(role)))
        database.get_guild_config.assert_awaited_once_with("123456789012345678")

    async def test_uses_legacy_role_only_when_not_configured(self):
        role = SimpleNamespace(id=99)
        database = SimpleNamespace(get_guild_config=AsyncMock(return_value={}))
        service = PermissionService(lambda: database, 99)
        self.assertTrue(await service.is_support_staff(self.make_member(roles=[role]), self.make_guild(role)))

    async def test_fails_closed_when_database_is_not_initialized(self):
        service = PermissionService(lambda: None, 99)
        self.assertFalse(await service.is_support_staff(self.make_member(), self.make_guild(None)))


if __name__ == "__main__":
    unittest.main()
