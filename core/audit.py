"""Small application service for attributable, append-only audit events."""

from core.database import CloudflareDatabase


class AuditService:
    def __init__(self, database: CloudflareDatabase):
        self.database = database

    async def record(self, *, guild_id: int | str, actor_id: int | str, action: str,
                     target_id: int | str | None = None, details=None, created_at: str):
        if not action or len(action) > 64:
            raise ValueError("Audit action must contain 1–64 characters")
        return await self.database.append_audit_event(
            guild_id=str(guild_id), actor_id=str(actor_id), action=action,
            target_id=str(target_id) if target_id is not None else None,
            details=details or {}, created_at=created_at,
        )

    async def recent(self, guild_id: int | str, *, limit: int = 10):
        return await self.database.list_audit_events(str(guild_id), limit=limit)
