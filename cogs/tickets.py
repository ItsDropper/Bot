"""Staff-only support ticket operations backed by persistent D1 activity."""

import json
from datetime import datetime, timezone

import discord
from discord import app_commands

from core.database import DatabaseError


def register_ticket_commands(tree, database_provider, support_only, is_support_staff, audit_recorder):
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

    @tree.command(name="ticketassign", description="Assign an open support ticket to a support staff member")
    @app_commands.describe(channel="Open support ticket", staff_member="Support staff member responsible for this ticket")
    @app_commands.guild_only()
    @support_only()
    async def ticketassign(
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        staff_member: discord.Member,
    ):
        if not channel.name.startswith("support-"):
            await interaction.response.send_message("Select an open support ticket channel.", ephemeral=True)
            return
        if not await is_support_staff(staff_member, interaction.guild):
            await interaction.response.send_message(
                "The assignee must have the configured Vanta support role or be a server administrator.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        assigned_at = datetime.now(timezone.utc).isoformat()
        try:
            ticket = await database().assign_support_ticket(
                guild_id=str(interaction.guild.id),
                channel_id=str(channel.id),
                assigned_to=str(staff_member.id),
                assigned_by=str(interaction.user.id),
                assigned_at=assigned_at,
            )
        except DatabaseError as exc:
            await interaction.followup.send(f"Could not assign this ticket: {exc}", ephemeral=True)
            return
        await audit_recorder(
            interaction.guild.id, interaction.user.id, "ticket.assigned",
            target_id=channel.id,
            details={"assigned_to": str(staff_member.id)},
        )
        await interaction.followup.send(
            f"{channel.mention} is now assigned to {staff_member.mention}.",
            ephemeral=True,
        )

    @tree.command(name="ticketqueue", description="View the prioritized open support ticket queue")
    @app_commands.guild_only()
    @support_only()
    async def ticketqueue(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            tickets, total = await database().list_support_tickets(
                str(interaction.guild.id), status="open", limit=25,
            )
        except DatabaseError as exc:
            await interaction.followup.send(f"Could not load the support queue: {exc}", ephemeral=True)
            return
        priority_rank = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
        tickets.sort(key=lambda row: (
            priority_rank.get(row.get("priority", "normal"), 2),
            row.get("created_at", ""),
        ))
        embed = discord.Embed(
            title="VANTA  /  SUPPORT QUEUE",
            description="Open tickets ordered by priority, then age.",
            color=0x3978F6,
        )
        if not tickets:
            embed.add_field(name="Queue clear", value="There are no open support tickets.")
        else:
            lines = []
            for ticket in tickets[:15]:
                channel = interaction.guild.get_channel(int(ticket["channel_id"]))
                channel_label = channel.mention if channel else f"`{ticket['channel_id']}`"
                priority = str(ticket.get("priority") or "normal").upper()
                assigned = f"<@{ticket['assigned_to']}>" if ticket.get("assigned_to") else "Unassigned"
                created = int(datetime.fromisoformat(ticket["created_at"].replace("Z", "+00:00")).timestamp())
                reason = discord.utils.escape_markdown(ticket["reason"])[:100]
                lines.append(
                    f"**{priority}** · {channel_label} · <@{ticket['owner_id']}>\n"
                    f"{assigned} · opened <t:{created}:R>\n"
                    f"{reason}"
                )
            embed.description = "\n\n".join(lines)
        embed.set_footer(text=f"{total} open ticket(s) · Showing up to 15 · Priority-first ordering")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @tree.command(name="ticketactivity", description="View ticket response, assignment, note and priority history")
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
                elif action == "assignment_changed":
                    assignee_id = details.get("assigned_to")
                    summary = f"**Assigned to** · <@{assignee_id}>" if assignee_id else "**Ticket unassigned**"
                elif action == "customer_message":
                    summary = "**Customer replied** · response timer restarted"
                elif action == "staff_reply":
                    summary = "**Staff response** · response recorded"
                elif action == "priority_changed":
                    summary = f"**Priority changed** · {str(details.get('priority', 'unknown')).upper()}"
                else:
                    summary = f"**{discord.utils.escape_markdown(action.replace('_', ' ').title())}**"
                timestamp = int(datetime.fromisoformat(event["created_at"].replace("Z", "+00:00")).timestamp())
                lines.append(f"{summary}\n<t:{timestamp}:R> · <@{event['actor_id']}>")
            embed.description = "\n\n".join(lines)
        embed.set_footer(text="Internal staff activity · Cloudflare D1")
        await interaction.followup.send(embed=embed, ephemeral=True)
