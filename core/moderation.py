"""Persistent moderation case service backed by the authenticated D1 API."""

import re
from datetime import datetime, timezone


DISCORD_ID = re.compile(r"^[0-9]{15,22}$")
ACTIONS = {"warn", "timeout", "kick", "ban", "unban"}
STATUSES = {"applied", "failed"}


class ModerationService:
    def __init__(self, database):
        self.database = database

    async def create_case(self, *, guild_id, target_id, moderator_id, action,
                          reason, created_at=None, expires_at=None):
        values = (guild_id, target_id, moderator_id)
        if any(not DISCORD_ID.fullmatch(str(value)) for value in values):
            raise ValueError("Guild, target and moderator IDs must be valid Discord IDs")
        if action not in ACTIONS:
            raise ValueError("Unsupported moderation action")
        if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 512:
            raise ValueError("Moderation reason must contain 1–512 characters")
        if expires_at is not None and not _valid_timestamp(expires_at):
            raise ValueError("Invalid moderation expiry timestamp")
        created_at = created_at or datetime.now(timezone.utc).isoformat()
        if not _valid_timestamp(created_at):
            raise ValueError("Invalid moderation creation timestamp")
        return await self.database.create_moderation_case(
            guild_id=str(guild_id), target_id=str(target_id),
            moderator_id=str(moderator_id), action=action,
            reason=reason.strip(), created_at=created_at, expires_at=expires_at,
        )

    async def complete_case(self, case_id, *, status, completed_at=None, failure_reason=None):
        if not isinstance(case_id, int) or case_id < 1:
            raise ValueError("Invalid moderation case ID")
        if status not in STATUSES:
            raise ValueError("A moderation case can only be completed as applied or failed")
        if failure_reason is not None:
            failure_reason = str(failure_reason).strip()[:500] or None
        return await self.database.complete_moderation_case(
            case_id, status=status,
            completed_at=completed_at or datetime.now(timezone.utc).isoformat(),
            failure_reason=failure_reason,
        )

    async def list_cases(self, guild_id, *, target_id=None, limit=10, offset=0):
        if not DISCORD_ID.fullmatch(str(guild_id)):
            raise ValueError("Invalid guild ID")
        if target_id is not None and not DISCORD_ID.fullmatch(str(target_id)):
            raise ValueError("Invalid target ID")
        if not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValueError("Moderation case page size must be between 1 and 25")
        if not isinstance(offset, int) or not 0 <= offset <= 1_000_000:
            raise ValueError("Invalid moderation case offset")
        return await self.database.list_moderation_cases(
            str(guild_id), target_id=str(target_id) if target_id else None,
            limit=limit, offset=offset,
        )


def _valid_timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False
