"""Centralized, fail-closed support authorization policies."""

import discord
from discord import app_commands


class PermissionService:
    def __init__(self, database_provider, fallback_support_role_id: int):
        self._database_provider = database_provider
        self._fallback_support_role_id = int(fallback_support_role_id)

    async def is_support_staff(self, member: discord.Member, guild: discord.Guild) -> bool:
        if guild is None or member is None:
            return False
        if member.guild_permissions.administrator:
            return True
        database = self._database_provider()
        if database is None:
            return False
        config = await database.get_guild_config(str(guild.id)) or {}
        role_id = int(config.get("support_role_id") or self._fallback_support_role_id)
        support_role = guild.get_role(role_id)
        return bool(support_role and support_role in member.roles)

    async def require_support_staff(self, interaction: discord.Interaction) -> bool:
        if await self.is_support_staff(interaction.user, interaction.guild):
            return True
        message = "Only support staff or administrators can use this control."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
        return False

    def support_only(self):
        async def predicate(interaction: discord.Interaction):
            if await self.is_support_staff(interaction.user, interaction.guild):
                return True
            await interaction.response.send_message(
                "You need the configured support role to use this command.",
                ephemeral=True,
            )
            return False
        return app_commands.check(predicate)
