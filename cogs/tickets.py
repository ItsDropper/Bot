"""Staff-only support ticket operations backed by persistent D1 activity."""

import json
from datetime import datetime, timezone

import discord
from discord import app_commands

from core.database import DatabaseError


def register_ticket_commands(tree, database_provider, support_only, audit_recorder):
    def database():
        value = database_provider()
        if value is None:
            raise DatabaseError("The database is not available")
        return value

    @tree.command(name="ticketnote", description="Add a private internal note to a support ticket")
    @app_commands.describe(channel="Open support ticket", note="Staff-only note (not visible to the ticket owner)")
    @app_commands.guild_only()
    @support_only()
    async def ticketnote(interaction: discord.Interaction, channel: discord.TextChannel,
                         note: app_commands.Range[str, 1, 1000]):
        if not channel.name.startswith("support-"):
            await interaction.response.send_message("Select an open support ticket channel.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await database().add_ticket_note(
                guild_id=str(interaction.guild.id), channel_id=str(channel.id),
                actor_id=str(interaction.user.id), note=note,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        except DatabaseError as exc:
            await interaction.followup.send(f"Could not save internal note: {exc}", ephemeral=True)
            return
        await audit_recorder(
            interaction.guild.id, interaction.user.id, "ticket.internal_note",
            target_id=channel.id, details={"note_length": len(note)},
        )
        await interaction.followup.send(
            f"Private note saved to {channel.mention}. It is stored in D1 and was not posted in the ticket.",
            ephemeral=True,
        )

    @tree.command(name="ticketpriority", description="Set the priority of an open support ticket")
    @app_commands.describe(channel="Open support ticket", priority="New ticket priority")
    @app_commands.choices(priority=[
        app_commands.Choice(name="Low", value="low"),
        app_commands.Choice(name="Normal", value="normal"),
        app_commands.Choice(name="High", value="high"),
        app_commands.Choice(name="Urgent", value="urgent"),
    ])
    @app_commands.guild_only()
    @support_only()
    async def ticketpriority(interaction: discord.Interaction, channel: discord.TextChannel,
                             priority: app_commands.Choice[str]):
        if not channel.name.startswith("support-"):
            await interaction.response.send_message("Select an open support ticket channel.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            ticket = await database().set_ticket_priority(
                guild_id=str(interaction.guild.id), channel_id=str(channel.id),
                actor_id=str(interaction.user.id), priority=priority.value,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        except DatabaseError as exc:
            await interaction.followup.send(f"Could not update ticket priority: {exc}", ephemeral=True)
            return
        await audit_recorder(
            interaction.guild.id, interaction.user.id, "ticket.priority_changed",
            target_id=channel.id, details={"priority": priority.value},
        )
        await interaction.followup.send(
            f"Priority for {channel.mention} set to **{ticket['priority'].upper()}**.",
            ephemeral=True,
        )

    @tree.command(name="ticketactivity", description="View staff notes and priority history for a support ticket")
    @app_commands.describe(channel="Support ticket to inspect")
    @app_commands.guild_only()
    @support_only()
    async def ticketactivity(interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        try:
            events = await database().list_ticket_activity(
                guild_id=str(interaction.guild.id), channel_id=str(channel.id), limit=15,
            )
        except DatabaseError as exc:
            await interaction.followup.send(f"Could not load ticket activity: {exc}", ephemeral=True)
            return
        embed = discord.Embed(
            title="VANTA  /  TICKET ACTIVITY",
            description=f"Staff-only notes and priority history for {channel.mention}",
            color=0x3978F6,
        )
        if not events:
            embed.add_field(name="No activity", value="No internal notes or priority changes are recorded yet.")
        else:
            lines = []
            for event in events:
                details = event.get("details") or "{}"
                if isinstance(details, str):
                    try:
                        details = json.loads(details)
                    except json.JSONDecodeError:
                        details = {}
                action = event["action"]
                if action == "internal_note":
                    note = discord.utils.escape_markdown(str(details.get("note", "")))[:220]
                    summary = f"**Internal note** · {note}"
                else:
                    summary = f"**Priority changed** · {str(details.get('priority', 'unknown')).upper()}"
                timestamp = int(datetime.fromisoformat(event["created_at"].replace("Z", "+00:00")).timestamp())
                lines.append(f"{summary}\n<t:{timestamp}:R> · <@{event['actor_id']}>")
            embed.description = "\n\n".join(lines)
        embed.set_footer(text="Internal staff activity · Cloudflare D1")
        await interaction.followup.send(embed=embed, ephemeral=True)
