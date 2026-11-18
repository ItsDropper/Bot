import os
import asyncio
import re
import aiohttp
from datetime import datetime
import discord
from discord import app_commands
from discord.ext import tasks
from aiohttp import web

from config.settings import Settings
from core.database import CloudflareDatabase, DatabaseError, MinecraftLinkConflict
from core.audit import AuditService
from core.permissions import PermissionService
from core.support import build_technical_report
from cogs.moderation import register_moderation_commands
from cogs.tickets import register_ticket_commands

# Application-owned Vanta emoji: use the PartialEmoji object for components and
# Discord's custom-emoji markup in embed text.
VANTA_EMOJI = discord.PartialEmoji.from_str("<:1000043856:1572713589750501617>")
VANTA_EMOJI_MARKUP = "<:1000043856:1572713589750501617>"

settings = Settings.from_env()
PORT = settings.port

TICKET_CATEGORY_ID = 1514689354281259170
SUPPORT_ROLE_ID = 1514274160329756673
TICKET_LOG_CHANNEL_ID = 1514004581410013264
LAUNCHER_ANNOUNCEMENT_CHANNEL_ID = 1530652249653645422
LAUNCHER_ANNOUNCEMENT_ROLE_ID = 1530664744929722418

intents = discord.Intents.default()

# ── Cloudflare D1 ─────────────────────────────────────────────────────────────

database: CloudflareDatabase | None = None
BOT_STARTED_AT = datetime.utcnow()

async def init_db():
    global database
    worker_url, api_token = settings.require_database_credentials()
    if database is None:
        candidate = CloudflareDatabase(worker_url, api_token)
        await candidate.start()
        try:
            await candidate.healthcheck()
        except Exception:
            await candidate.close()
            raise
        database = candidate
    else:
        await database.healthcheck()
    print("Cloudflare D1 connection verified.")

async def record_audit(guild_id: int, actor_id: int, action: str, *, target_id=None, details=None):
    """Persist an attributable operational event without masking the primary operation."""
    try:
        await AuditService(database).record(
            guild_id=guild_id, actor_id=actor_id, action=action,
            target_id=target_id, details=details or {}, created_at=datetime.utcnow().isoformat(),
        )
    except Exception as exc:
        print(f"Audit persistence failed for {action} in guild {guild_id}: {type(exc).__name__}: {exc}")




async def close_db():
    global database
    if database is not None:
        await database.close()
        database = None


# ── Permission checks ────────────────────────────────────────────────────────

# ── Permission check ──────────────────────────────────────────────────────────

permission_service = PermissionService(lambda: database, SUPPORT_ROLE_ID)
is_support_staff = permission_service.is_support_staff
require_support_staff = permission_service.require_support_staff
support_only = permission_service.support_only

# ── Support ticket close control ──────────────────────────────────────────────

class CloseSupportTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _close(self, interaction: discord.Interaction):
        topic = interaction.channel.topic or ""
        owner_match = re.fullmatch(r"vanta_ticket_owner:([0-9]{15,22})", topic)
        is_owner = bool(owner_match and int(owner_match.group(1)) == interaction.user.id)
        if not is_owner and not await is_support_staff(interaction.user, interaction.guild):
            await interaction.response.send_message(
                "Only the ticket creator or support staff can close this ticket.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        if interaction.channel.name.startswith("support-"):
            try:
                await database.close_support_ticket(
                    str(interaction.channel.id), str(interaction.user.id), datetime.utcnow().isoformat()
                )
            except DatabaseError as exc:
                if str(exc) != "Ticket not found":
                    await interaction.followup.send(f"Could not close ticket safely: {exc}", ephemeral=True)
                    return
                print(f"Closing legacy ticket {interaction.channel.id} without a D1 record.")
        config = await database.get_guild_config(str(interaction.guild.id)) or {}
        log_channel_id = int(config.get("ticket_log_channel_id") or TICKET_LOG_CHANNEL_ID)
        if log_channel_id:
            log_channel = interaction.guild.get_channel(log_channel_id)
            if log_channel:
                embed = discord.Embed(title="Ticket Closed", color=discord.Color.red())
                embed.add_field(name="Channel", value=interaction.channel.name, inline=True)
                embed.add_field(name="Closed by", value=interaction.user.mention, inline=True)
                embed.set_footer(text=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))
                await log_channel.send(embed=embed)
        await record_audit(interaction.guild.id, interaction.user.id, "ticket.closed", target_id=interaction.channel.id)
        await interaction.followup.send('Ticket closed and recorded in the database.', ephemeral=True)
        await interaction.channel.delete()

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger,
                       custom_id="support_ticket_close", emoji="🔒")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._close(interaction)



# ── Support ticket system ─────────────────────────────────────────────────────

async def _create_support_ticket(
    interaction: discord.Interaction,
    topic: str,
    details: str,
    *,
    persisted_details: str | None = None,
    technical_fields: list[tuple[str, str]] | None = None,
):
    ticket_reason = f"[{topic}] {persisted_details if persisted_details is not None else details}"
    if len(ticket_reason) > 1000:
        await interaction.response.send_message(
            "The support report is too long. Shorten the details or log link and submit again.",
            ephemeral=True,
        )
        return

    guild = interaction.guild
    config = await database.get_guild_config(str(guild.id)) or {}
    category_id = int(config.get("support_category_id") or TICKET_CATEGORY_ID)
    support_role_id = int(config.get("support_role_id") or SUPPORT_ROLE_ID)
    category = guild.get_channel(category_id)

    # D1 is the source of truth for duplicate prevention; this channel check
    # also catches legacy tickets created before the persistent ticket ledger.
    if category:
        for ch in category.channels:
            if ch.name.startswith("support-"):
                overwrites_for_user = ch.overwrites_for(interaction.user)
                if overwrites_for_user.view_channel is True:
                    await interaction.response.send_message(
                        f"You already have an open support ticket: {ch.mention}", ephemeral=True
                    )
                    return

    support_role = guild.get_role(support_role_id)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_channels=True
        ),
    }
    if support_role:
        overwrites[support_role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        )

    safe_name = re.sub(r"[^a-z0-9-]", "", interaction.user.name.lower().replace(" ", "-"))[:20] or "member"
    await interaction.response.defer(ephemeral=True)
    channel = await guild.create_text_channel(
        name=f"support-{safe_name}",
        category=category,
        overwrites=overwrites,
        topic=f"vanta_ticket_owner:{interaction.user.id}"
    )
    try:
        await database.create_support_ticket(
            ticket_id=f"{guild.id}:{channel.id}",
            guild_id=str(guild.id),
            channel_id=str(channel.id),
            owner_id=str(interaction.user.id),
            reason=ticket_reason,
            created_at=datetime.utcnow().isoformat(),
        )
    except DatabaseError as exc:
        await channel.delete(reason="Ticket database registration failed")
        await interaction.followup.send(
            f"Ticket could not be registered. No ticket was left open. {exc}", ephemeral=True
        )
        return

    embed = discord.Embed(
        title=f"{VANTA_EMOJI_MARKUP}  NEW SUPPORT REQUEST",
        description="A private support conversation has been opened.",
        color=0x3978F6,
    )
    embed.add_field(name="REQUESTER", value=interaction.user.mention, inline=True)
    embed.add_field(name="TOPIC", value=topic, inline=True)
    embed.add_field(
        name="DETAILS",
        value=discord.utils.escape_mentions(discord.utils.escape_markdown(details)),
        inline=False,
    )
    for field_name, field_value in technical_fields or []:
        embed.add_field(
            name=field_name.upper(),
            value=discord.utils.escape_mentions(discord.utils.escape_markdown(field_value)),
            inline=False,
        )
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    embed.set_footer(text="VANTA SUPPORT  •  Keep all updates in this channel")
    embed.timestamp = datetime.utcnow()

    ping = support_role.mention if support_role else None
    await channel.send(
        content=ping,
        embed=embed,
        view=CloseSupportTicketView(),
        allowed_mentions=discord.AllowedMentions(roles=[support_role] if support_role else [], users=False),
    )
    await record_audit(
        guild.id, interaction.user.id, "ticket.created",
        target_id=channel.id,
        details={"owner_id": str(interaction.user.id), "topic": topic,
                 "technical_intake": bool(technical_fields)},
    )
    await interaction.followup.send(
        f"Your support ticket is ready: {channel.mention}", ephemeral=True
    )


class SupportTicketModal(discord.ui.Modal, title="Create Support Ticket"):
    reason = discord.ui.TextInput(
        label="Describe what you need help with",
        style=discord.TextStyle.paragraph,
        placeholder="Include what you were doing, what happened and any error message...",
        max_length=1000,
        min_length=10,
    )

    def __init__(self, topic: str = "General support"):
        super().__init__()
        self.topic = topic

    async def on_submit(self, interaction: discord.Interaction):
        await _create_support_ticket(interaction, self.topic, self.reason.value)


class TechnicalSupportTicketModal(discord.ui.Modal, title="Vanta / Modding Support"):
    minecraft_version = discord.ui.TextInput(
        label="Minecraft version (if applicable)",
        placeholder="e.g. 1.21.11 — leave blank if unknown",
        required=False,
        max_length=32,
    )
    loader = discord.ui.TextInput(
        label="Loader + version",
        placeholder="e.g. Fabric 0.x, NeoForge, Quilt, Rift...",
        required=False,
        max_length=80,
    )
    launcher_version = discord.ui.TextInput(
        label="Vanta Launcher version",
        placeholder="Version shown in the launcher",
        required=False,
        max_length=40,
    )
    summary = discord.ui.TextInput(
        label="What happened?",
        style=discord.TextStyle.paragraph,
        placeholder="Steps to reproduce, expected vs actual result, modpack and relevant mods...",
        min_length=10,
        max_length=300,
    )
    log_url = discord.ui.TextInput(
        label="Log / crash report URL (optional)",
        placeholder="Paste a share link; remove tokens or private information first",
        required=False,
        max_length=300,
    )

    def __init__(self, topic: str):
        super().__init__()
        self.topic = topic

    async def on_submit(self, interaction: discord.Interaction):
        try:
            persisted, fields = build_technical_report(
                minecraft_version=self.minecraft_version.value,
                loader=self.loader.value,
                launcher_version=self.launcher_version.value,
                summary=self.summary.value,
                log_url=self.log_url.value,
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await _create_support_ticket(
            interaction,
            self.topic,
            self.summary.value.strip(),
            persisted_details=persisted,
            technical_fields=fields,
        )


class SupportPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        # Persistent views are shared across users; keep selection state keyed by
        # Discord user ID so one member cannot change another member's ticket topic.
        self.selected_topics: dict[tuple[int, int], str] = {}

    @discord.ui.select(
        cls=discord.ui.Select,
        placeholder="Choose what you need help with",
        min_values=1,
        max_values=1,
        custom_id="support_ticket_topic",
        options=[
            discord.SelectOption(label="Launcher & installation", value="Launcher & installation", emoji="🚀"),
            discord.SelectOption(label="Account & access", value="Account & access", emoji="🔐"),
            discord.SelectOption(label="Mods & compatibility", value="Mods & compatibility", emoji="🧩"),
            discord.SelectOption(label="Bug report", value="Bug report", emoji="🐛"),
            discord.SelectOption(label="Other question", value="General support", emoji="💬"),
        ],
        row=0,
    )
    async def choose_topic(self, interaction: discord.Interaction, select: discord.ui.Select):
        topic = select.values[0]
        self.selected_topics[(interaction.guild_id, interaction.user.id)] = topic
        await interaction.response.send_message(
            f"Support topic set to **{discord.utils.escape_markdown(topic)}**. Select Create ticket when you're ready.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Create ticket",
        style=discord.ButtonStyle.secondary,
        custom_id="support_ticket_open",
        emoji=VANTA_EMOJI,
        row=1,
    )
    async def open_support_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        topic = self.selected_topics.pop((interaction.guild_id, interaction.user.id), "General support")
        technical_topics = {"Launcher & installation", "Mods & compatibility", "Bug report"}
        if topic in technical_topics:
            await interaction.response.send_modal(TechnicalSupportTicketModal(topic))
        else:
            await interaction.response.send_modal(SupportTicketModal(topic))

    @discord.ui.button(
        label="My open tickets",
        style=discord.ButtonStyle.secondary,
        custom_id="support_ticket_my_tickets",
        emoji="📂",
        row=1,
    )
    async def my_open_tickets(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            tickets, _ = await database.list_support_tickets(
                str(interaction.guild.id), status="open", limit=25,
            )
        except DatabaseError as exc:
            await interaction.followup.send(
                f"Ticket history is temporarily unavailable: {exc}", ephemeral=True
            )
            return
        owned = [ticket for ticket in tickets if str(ticket.get("owner_id")) == str(interaction.user.id)]
        embed = discord.Embed(
            title="YOUR OPEN SUPPORT TICKETS",
            description="Your active support conversations in this server.",
            color=0x3978F6,
        )
        if not owned:
            embed.description = "You do not have any open support tickets in this server."
        else:
            for ticket in owned[:10]:
                channel = interaction.guild.get_channel(int(ticket["channel_id"]))
                value = channel.mention if channel else "Channel unavailable"
                embed.add_field(
                    name=f"Ticket · {str(ticket.get('priority') or 'normal').upper()} priority",
                    value=value,
                    inline=False,
                )
        embed.set_footer(text="VANTA SUPPORT  •  Only your own tickets are shown")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="Support guide",
        style=discord.ButtonStyle.secondary,
        custom_id="support_ticket_guide",
        emoji="📘",
        row=1,
    )
    async def support_guide(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="HOW VANTA SUPPORT WORKS",
            description="A short guide to getting a useful answer from the support team.",
            color=0x3978F6,
        )
        embed.add_field(
            name="01  ·  CREATE",
            value="Choose the closest topic, then describe the issue and what you have already tried.",
            inline=False,
        )
        embed.add_field(
            name="02  ·  WORK WITH SUPPORT",
            value="Your channel is private to you and authorized support staff. Reply in that channel if staff ask for more information.",
            inline=False,
        )
        embed.add_field(
            name="03  ·  CLOSE",
            value="Use the Close Ticket control when the issue is resolved. You can open another ticket later if needed.",
            inline=False,
        )
        embed.add_field(
            name="HELPFUL DETAILS",
            value="For launcher, mod and bug reports, the form collects Minecraft version, loader, launcher version, reproduction details and an optional log link. Remove access tokens and private information before sharing logs.",
            inline=False,
        )
        embed.set_footer(text="VANTA SUPPORT  •  One issue per ticket")
        await interaction.response.send_message(embed=embed, ephemeral=True)



APPLICATION_RESULT_CHANNEL_ID = 1530651965908848641

class StaffApplicationModal(discord.ui.Modal, title="Staff Application"):
    age = discord.ui.TextInput(label="How old are you?", placeholder="Your age", max_length=20)
    experience = discord.ui.TextInput(
        label="Previous staff / moderation experience?",
        style=discord.TextStyle.paragraph,
        placeholder="Describe any servers you've staffed, your role, how long, etc.",
        max_length=500
    )
    timezone = discord.ui.TextInput(
        label="Timezone & daily availability",
        placeholder="e.g. EST — available 4–10 PM on weekdays",
        max_length=80
    )
    why = discord.ui.TextInput(
        label="Why do you want to be staff here?",
        style=discord.TextStyle.paragraph,
        placeholder="Be specific and genuine.",
        max_length=1000
    )
    extra = discord.ui.TextInput(
        label="Anything else to add? (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=400
    )

    async def on_submit(self, interaction: discord.Interaction):
        config = await database.get_guild_config(str(interaction.guild.id)) or {}
        application_channel_id = int(config.get("application_result_channel_id") or APPLICATION_RESULT_CHANNEL_ID)
        channel = interaction.guild.get_channel(application_channel_id)
        if not channel:
            await interaction.response.send_message("Application channel not found. Contact an admin.", ephemeral=True)
            return
        embed = discord.Embed(title=f"{VANTA_EMOJI_MARKUP}  NEW STAFF APPLICATION", color=0x3978F6)
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name="Applicant", value=f"{interaction.user.mention} (`{interaction.user.name}`)", inline=False)
        embed.add_field(name="Age", value=self.age.value, inline=True)
        embed.add_field(name="Timezone / Availability", value=self.timezone.value, inline=True)
        embed.add_field(name="Experience", value=self.experience.value, inline=False)
        embed.add_field(name="Why do they want to be staff?", value=self.why.value, inline=False)
        if self.extra.value:
            embed.add_field(name="Additional Info", value=self.extra.value, inline=False)
        embed.set_footer(text=f"Application type: staff | User ID: {interaction.user.id}")
        embed.timestamp = datetime.utcnow()
        await channel.send(embed=embed, view=ApplicationResultView())
        await interaction.response.send_message(
            "✅ Your staff application has been submitted! You'll be notified of the decision.", ephemeral=True
        )


class DenyReasonModal(discord.ui.Modal, title="Deny with Reason"):
    reason_input = discord.ui.TextInput(
        label="Denial Reason",
        style=discord.TextStyle.paragraph,
        placeholder="Explain why the application is denied...",
        max_length=500
    )

    def __init__(self, applicant_id: str, app_type: str, original_message: discord.Message):
        super().__init__()
        self.applicant_id = applicant_id
        self.app_type = app_type
        self.original_message = original_message

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_support_staff(interaction):
            return
        reason = self.reason_input.value
        try:
            applicant = await client.fetch_user(int(self.applicant_id))
            dm_embed = discord.Embed(
                title="Application Update",
                description=f"Your **{self.app_type}** application in **{interaction.guild.name}** was **denied**.",
                color=discord.Color.red()
            )
            dm_embed.add_field(name="Reason", value=reason, inline=False)
            await applicant.send(embed=dm_embed)
        except (discord.Forbidden, discord.NotFound):
            pass
        orig = self.original_message.embeds[0]
        orig.color = discord.Color.red()
        orig.add_field(name="❌ Denied by", value=f"{interaction.user.mention}\n**Reason:** {reason}", inline=False)
        await self.original_message.edit(embed=orig, view=None)
        await record_audit(interaction.guild.id, interaction.user.id, "application.denied", target_id=int(self.applicant_id), details={"type": self.app_type, "reason": reason})
        await interaction.response.send_message("❌ Application denied with reason sent.", ephemeral=True)


class ApplicationResultView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    def _parse(self, message: discord.Message):
        if not message.embeds:
            return None, None
        footer = message.embeds[0].footer.text or ""
        app_type, user_id = None, None
        for part in footer.split("|"):
            part = part.strip()
            if part.startswith("Application type:"):
                app_type = part.replace("Application type:", "").strip()
            elif part.startswith("User ID:"):
                user_id = part.replace("User ID:", "").strip()
        return user_id, app_type

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, custom_id="app_result_approve", emoji="✅")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_support_staff(interaction):
            return
        user_id, app_type = self._parse(interaction.message)
        if not user_id:
            await interaction.response.send_message("Could not read applicant info from embed.", ephemeral=True)
            return
        try:
            member = interaction.guild.get_member(int(user_id))
            if not member:
                member = await interaction.guild.fetch_member(int(user_id))
            try:
                applicant = await client.fetch_user(int(user_id))
                dm_embed = discord.Embed(
                    title="🎉 Application Approved!",
                    description=f"Your **{app_type}** application in **{interaction.guild.name}** has been **approved**!",
                    color=discord.Color.green()
                )
                await applicant.send(embed=dm_embed)
            except (discord.Forbidden, discord.NotFound):
                pass
        except Exception as e:
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)
            return
        orig = interaction.message.embeds[0]
        orig.color = discord.Color.green()
        orig.add_field(name="✅ Approved by", value=interaction.user.mention, inline=False)
        await interaction.message.edit(embed=orig, view=None)
        await record_audit(interaction.guild.id, interaction.user.id, "application.approved", target_id=int(user_id), details={"type": app_type})
        await interaction.response.send_message("✅ Application approved!", ephemeral=True)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, custom_id="app_result_deny", emoji="❌")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_support_staff(interaction):
            return
        user_id, app_type = self._parse(interaction.message)
        if not user_id:
            await interaction.response.send_message("Could not read applicant info from embed.", ephemeral=True)
            return
        try:
            applicant = await client.fetch_user(int(user_id))
            dm_embed = discord.Embed(
                title="Application Update",
                description=f"Your **{app_type}** application in **{interaction.guild.name}** was **denied**.",
                color=discord.Color.red()
            )
            await applicant.send(embed=dm_embed)
        except (discord.Forbidden, discord.NotFound):
            pass
        orig = interaction.message.embeds[0]
        orig.color = discord.Color.red()
        orig.add_field(name="❌ Denied by", value=interaction.user.mention, inline=False)
        await interaction.message.edit(embed=orig, view=None)
        await record_audit(interaction.guild.id, interaction.user.id, "application.denied", target_id=int(user_id), details={"type": app_type})
        await interaction.response.send_message("❌ Application denied.", ephemeral=True)

    @discord.ui.button(label="Private Ticket", style=discord.ButtonStyle.secondary, custom_id="app_result_ticket", emoji="💬")
    async def private_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_support_staff(interaction):
            return
        user_id, app_type = self._parse(interaction.message)
        if not user_id:
            await interaction.response.send_message("Could not read applicant info from embed.", ephemeral=True)
            return
        member = interaction.guild.get_member(int(user_id))
        if not member:
            try:
                member = await interaction.guild.fetch_member(int(user_id))
            except discord.NotFound:
                await interaction.response.send_message("Applicant is no longer in the server.", ephemeral=True)
                return
        config = await database.get_guild_config(str(interaction.guild.id)) or {}
        category_id = int(config.get("support_category_id") or TICKET_CATEGORY_ID)
        category = interaction.guild.get_channel(category_id)
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            interaction.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        }
        safe_name = member.name.lower().replace(" ", "-")[:20]
        channel = await interaction.guild.create_text_channel(
            name=f"app-{safe_name}",
            category=category,
            overwrites=overwrites,
            topic=f"vanta_ticket_owner:{member.id}"
        )
        embed = discord.Embed(
            title="Application Discussion",
            description=f"Opened to discuss {member.mention}'s **{app_type}** application.",
            color=discord.Color.blurple()
        )
        await channel.send(content=f"{member.mention} {interaction.user.mention}", embed=embed, view=CloseSupportTicketView())
        await interaction.response.send_message(f"💬 Private ticket created: {channel.mention}", ephemeral=True)

    @discord.ui.button(label="Deny with Reason", style=discord.ButtonStyle.danger, custom_id="app_result_deny_reason", emoji="📝")
    async def deny_with_reason(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_support_staff(interaction):
            return
        user_id, app_type = self._parse(interaction.message)
        if not user_id:
            await interaction.response.send_message("Could not read applicant info from embed.", ephemeral=True)
            return
        await interaction.response.send_modal(DenyReasonModal(user_id, app_type, interaction.message))


class ApplicationPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Apply — Vanta Staff", style=discord.ButtonStyle.secondary,
                       custom_id="app_open_staff", emoji=VANTA_EMOJI)
    async def apply_staff(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(StaffApplicationModal())


class LegacyTesterApplicationView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Tester Applications Closed", style=discord.ButtonStyle.secondary,
                       custom_id="app_open_tester")
    async def retired(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Tester applications have been retired. Please use the current Vanta staff application panel.",
            ephemeral=True
        )


class LegacyTierPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Tier Testing Retired", style=discord.ButtonStyle.secondary,
                       custom_id="tier_test_open")
    async def retired_open(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "The old tier-testing system has been retired. Use the Vanta support panel if you need help.",
            ephemeral=True
        )

    @discord.ui.button(label="Tier Cooldowns Retired", style=discord.ButtonStyle.secondary,
                       custom_id="tier_check_cooldown")
    async def retired_cooldowns(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("The old tier-testing system has been retired.", ephemeral=True)


# ── Bot setup ─────────────────────────────────────────────────────────────────

async def _announce_latest_launcher_release(guild: discord.Guild) -> str:
    config = await database.get_guild_config(str(guild.id)) or {}
    announcement_channel_id = int(config.get("launcher_announcement_channel_id") or LAUNCHER_ANNOUNCEMENT_CHANNEL_ID)
    announcement_role_id = int(config.get("launcher_announcement_role_id") or LAUNCHER_ANNOUNCEMENT_ROLE_ID)
    channel = guild.get_channel(announcement_channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(announcement_channel_id)
        except (discord.NotFound, discord.Forbidden, discord.InvalidData):
            # A configured/default channel may belong to a different guild when this
            # bot is installed in multiple servers. Never let that break startup or sync.
            return "announcement channel is unavailable in this server"
    if not isinstance(channel, discord.TextChannel):
        return "announcement destination is not a text channel"

    role = guild.get_role(announcement_role_id)
    if role is None:
        return "announcement role not found"

    bot_member = guild.me
    if bot_member is None:
        return "bot member is unavailable"
    permissions = channel.permissions_for(bot_member)
    if not all((permissions.view_channel, permissions.read_message_history,
                permissions.send_messages, permissions.embed_links)):
        return "missing channel permissions"
    if not role.mentionable and not permissions.mention_everyone:
        return "missing role mention permission"

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                "https://api.github.com/repos/ItsDropper/Vanta/releases/latest",
                headers={"Accept": "application/vnd.github+json", "User-Agent": "Vanta-Discord-Bot"},
            ) as response:
                if response.status != 200:
                    return f"GitHub release lookup returned HTTP {response.status}"
                release = await response.json()
        if not isinstance(release, dict):
            return "GitHub returned invalid release metadata"
        tag = release.get("tag_name")
        release_url = release.get("html_url")
        if (not isinstance(tag, str) or not tag or not isinstance(release_url, str)
                or not release_url.startswith("https://github.com/ItsDropper/Vanta/releases/")):
            return "GitHub returned invalid release metadata"
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        print(f"Could not fetch the latest Vanta release: {exc}")
        return "GitHub release lookup failed"

    try:
        existing_announcement = await database.get_release_announcement(str(guild.id), "launcher", tag)
    except DatabaseError as exc:
        print(f"Release ledger unavailable for guild {guild.id}: {exc}")
        return "release delivery ledger unavailable"
    if existing_announcement:
        return f"{tag} already announced (persistent D1 record)"

    title = f"Vanta Launcher Update — {tag}"
    async for previous in channel.history(limit=100):
        if previous.author.id == client.user.id and any(
            embed.title == title for embed in previous.embeds
        ):
            return f"{tag} already announced"

    notes = release.get("body")
    if not isinstance(notes, str) or not notes.strip():
        notes = "See the GitHub release page for the full changelog."
    else:
        notes = notes.strip()
    if len(notes) > 3000:
        notes = notes[:2997].rstrip() + "..."
    embed = discord.Embed(
        title=title,
        description=f"{notes}\n\n[Download Vanta Launcher and view release details]({release_url})",
        url=release_url,
        color=0x3978F6,
    )
    embed.set_footer(text="Vanta Launcher • Official Release")
    published_at = release.get("published_at")
    if published_at:
        try:
            embed.timestamp = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        except ValueError:
            pass

    try:
        message = await channel.send(
            content=role.mention,
            embed=embed,
            allowed_mentions=discord.AllowedMentions(
                roles=[role], users=False, everyone=False, replied_user=False
            ),
        )
    except discord.Forbidden:
        return "Discord rejected the announcement"
    try:
        await database.record_release_announcement(
            guild_id=str(guild.id), project="launcher", release_tag=tag,
            channel_id=str(channel.id), message_id=str(message.id),
            announced_at=datetime.utcnow().isoformat(),
        )
    except DatabaseError as exc:
        print(f"Release {tag} was posted but its D1 delivery record failed: {exc}")
        return f"announced {tag}, but delivery ledger write failed"
    return f"announced {tag} as message {message.id}"


_release_announcement_lock = asyncio.Lock()


async def announce_latest_launcher_release(guild: discord.Guild) -> str:
    """Serialize release checks so startup and the hourly monitor cannot double-post."""
    async with _release_announcement_lock:
        return await _announce_latest_launcher_release(guild)


class Client(discord.Client):
    async def setup_hook(self):
        self.add_view(CloseSupportTicketView())
        self.add_view(SupportPanelView())
        self.add_view(ApplicationPanelView())
        self.add_view(LegacyTesterApplicationView())
        self.add_view(LegacyTierPanelView())
        self.add_view(ApplicationResultView())
        self.launcher_release_announcer.start()

    @tasks.loop(hours=1)
    async def launcher_release_announcer(self):
        for guild in self.guilds:
            try:
                result = await announce_latest_launcher_release(guild)
                print(f"Launcher release monitor ({guild.id}): {result}")
            except Exception:
                import traceback
                traceback.print_exc()

    @launcher_release_announcer.before_loop
    async def before_launcher_release_announcer(self):
        await self.wait_until_ready()

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        synced_guilds = 0
        for guild in self.guilds:
            # Keep each guild's sync independent: one malformed/permission-limited
            # guild must not prevent command registration in the remaining servers.
            try:
                tree.copy_global_to(guild=guild)
                synced = await tree.sync(guild=guild)
                synced_guilds += 1
                command_names = ", ".join(sorted(command.name for command in synced))
                print(
                    f"Slash commands synced for {guild.name} ({guild.id}): "
                    f"{len(synced)} command(s) [{command_names}]"
                )
            except Exception as exc:
                print(
                    f"Slash command sync failed for {guild.name} ({guild.id}): "
                    f"{type(exc).__name__}: {exc}"
                )
                import traceback
                traceback.print_exc()
                continue

            try:
                await reconcile_ticket_close_controls(guild)
            except Exception as exc:
                print(f"Ticket control reconciliation failed in {guild.id}: {type(exc).__name__}: {exc}")

            try:
                result = await announce_latest_launcher_release(guild)
                print(f"Initial launcher release check ({guild.id}): {result}")
            except Exception as exc:
                # Announcement delivery is optional and must never interfere with
                # application-command availability.
                print(f"Initial launcher release check failed ({guild.id}): {type(exc).__name__}: {exc}")

        print(f"Slash command sync completed for {synced_guilds}/{len(self.guilds)} guild(s).")

async def reconcile_ticket_close_controls(guild: discord.Guild):
    """Replace old ticket messages that exposed both close controls with one control."""
    config = await database.get_guild_config(str(guild.id)) or {}
    category_id = int(config.get("support_category_id") or TICKET_CATEGORY_ID)
    category = guild.get_channel(category_id)
    if not isinstance(category, discord.CategoryChannel):
        return
    updated = 0
    for channel in category.text_channels:
        if not (channel.name.startswith("support-") or channel.name.startswith("app-")):
            continue
        try:
            async for message in channel.history(limit=200):
                if message.author.id != client.user.id:
                    continue
                custom_ids = {
                    component.custom_id
                    for row in message.components
                    for component in row.children
                    if getattr(component, "custom_id", None)
                }
                if "tier_ticket_close" in custom_ids:
                    await message.edit(view=CloseSupportTicketView())
                    updated += 1
                    break
        except (discord.Forbidden, discord.HTTPException) as exc:
            print(f"Could not reconcile ticket controls in #{channel.name}: {exc}")
    if updated:
        print(f"Replaced duplicate/legacy close controls in {updated} ticket channel(s).")


client = Client(intents=intents)
tree = app_commands.CommandTree(client)
register_moderation_commands(tree, lambda: database, record_audit)
register_ticket_commands(tree, lambda: database, support_only, record_audit)

@client.event
async def on_guild_join(guild):
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)
    print(f"Synced commands to {guild.name}")

@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    # Unwrap CheckFailure — permission checks already send their own message
    if isinstance(error, app_commands.CheckFailure):
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "You do not have permission to use this command.", ephemeral=True
            )
        return

    # Log to console
    import traceback
    print(f"Command error in /{interaction.command.name if interaction.command else '?'}:")
    traceback.print_exception(type(error), error, error.__traceback__)

    # Tell the user something went wrong
    msg = "❌ Something went wrong running that command. Please try again or contact an admin."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass

# ── Commands ──────────────────────────────────────────────────────────────────

@tree.command(name="unlink", description="Unlink your Minecraft account from Discord")
async def unlink(interaction: discord.Interaction):
    row = await database.get_minecraft_link(str(interaction.user.id))
    if not row:
        await interaction.response.send_message("You don't have a Minecraft account linked.", ephemeral=True)
        return
    await database.delete_minecraft_link(str(interaction.user.id))
    await interaction.response.send_message(f"✅ Unlinked `{row['mc_username']}` from your Discord account.", ephemeral=True)
@tree.command(name="mylink", description="Check your own linked Minecraft account")
async def mylink(interaction: discord.Interaction):
    row = await database.get_minecraft_link(str(interaction.user.id))
    if not row:
        await interaction.response.send_message("You haven't linked a Minecraft account yet. Use `/link <username>` to link one.", ephemeral=True)
        return
    embed = discord.Embed(title="Your Minecraft Link", color=discord.Color.green())
    embed.add_field(name="IGN", value=f"`{row['mc_username']}`", inline=True)
    embed.add_field(name="Linked", value=row["linked_at"][:10], inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)
@tree.command(name="announce", description="Send an announcement embed to a channel")
@app_commands.describe(channel="Channel to send to", title="Announcement title", message="Announcement content", color="Color (red/green/blue/gold/default)")
@app_commands.choices(color=[
    app_commands.Choice(name="Blue", value="blue"),
    app_commands.Choice(name="Green", value="green"),
    app_commands.Choice(name="Red", value="red"),
    app_commands.Choice(name="Gold", value="gold"),
])
async def announce(interaction: discord.Interaction, channel: discord.TextChannel, title: str, message: str, color: app_commands.Choice[str] = None):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You need Administrator permission to use this.", ephemeral=True)
        return
    colors = {"blue": discord.Color.blue(), "green": discord.Color.green(), "red": discord.Color.red(), "gold": discord.Color.gold()}
    c = colors.get(color.value if color else "blue", discord.Color.blue())
    embed = discord.Embed(title=title, description=message, color=c)
    embed.set_footer(text=f"Posted by {interaction.user.display_name}")
    await channel.send(embed=embed)
    await interaction.response.send_message(f"✅ Announcement sent to {channel.mention}.", ephemeral=True)


@tree.command(name="announce_launcher_update", description="Announce the latest Vanta Launcher release")
async def announce_launcher_update(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You need Administrator permission to use this.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    result = await announce_latest_launcher_release(interaction.guild)
    await interaction.followup.send(f"Launcher announcement: {result}.", ephemeral=True)


@tree.command(name="say", description="Make the bot send a message to a channel")
@app_commands.describe(channel="Channel to send to", message="The message to send")
async def say(interaction: discord.Interaction, channel: discord.TextChannel, message: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You need Administrator permission to use this.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        await channel.send(message)
        await interaction.followup.send(f"✅ Sent to {channel.mention}.", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send(f"❌ I don't have permission to send messages in {channel.mention}.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to send: {e}", ephemeral=True)


@tree.command(name="healthcheck", description="Show bot latency, database status, and system info")
async def healthcheck(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You need Administrator permission to use this.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    ws_latency = round(client.latency * 1000)
    import time
    start = time.monotonic()
    try:
        await database.healthcheck()
        db_status = f"✅ Cloudflare D1 ({round((time.monotonic() - start) * 1000)}ms)"
    except DatabaseError:
        db_status = "❌ Cloudflare D1 unavailable"
    guilds = len(client.guilds)
    members = sum(g.member_count or 0 for g in client.guilds)
    latency_icon = "🟢" if ws_latency < 100 else "🟡" if ws_latency < 250 else "🔴"
    embed = discord.Embed(title="System Health", color=discord.Color.green())
    embed.add_field(name="WebSocket Latency", value=f"{latency_icon} {ws_latency}ms", inline=True)
    embed.add_field(name="Database", value=db_status, inline=True)
    embed.add_field(name="Guilds", value=str(guilds), inline=True)
    embed.add_field(name="Total Members", value=str(members), inline=True)
    embed.add_field(name="Discord.py", value=discord.__version__, inline=True)
    embed.set_footer(text="All times in milliseconds")
    await interaction.followup.send(embed=embed, ephemeral=True)
@tree.command(name="permissioncheck", description="Show your permissions in this server")
@app_commands.describe(user="User to check (leave empty for yourself)")
async def permissioncheck(interaction: discord.Interaction, user: discord.Member = None):
    target = user or interaction.user
    perms = target.guild_permissions

    PERM_LABELS = {
        "administrator": "Administrator",
        "manage_guild": "Manage Server",
        "manage_roles": "Manage Roles",
        "manage_channels": "Manage Channels",
        "manage_messages": "Manage Messages",
        "manage_nicknames": "Manage Nicknames",
        "kick_members": "Kick Members",
        "ban_members": "Ban Members",
        "mention_everyone": "Mention Everyone",
        "view_audit_log": "View Audit Log",
        "send_messages": "Send Messages",
        "read_message_history": "Read Message History",
        "embed_links": "Embed Links",
        "attach_files": "Attach Files",
        "use_application_commands": "Use Slash Commands",
        "moderate_members": "Timeout Members",
        "move_members": "Move Members (VC)",
        "mute_members": "Mute Members (VC)",
    }

    granted = [label for attr, label in PERM_LABELS.items() if getattr(perms, attr, False)]
    denied = [label for attr, label in PERM_LABELS.items() if not getattr(perms, attr, False)]

    embed = discord.Embed(
        title=f"Permissions — {target.display_name}",
        color=discord.Color.green() if perms.administrator else discord.Color.blurple()
    )
    embed.add_field(
        name=f"✅ Granted ({len(granted)})",
        value="\n".join(granted) if granted else "None",
        inline=True
    )
    embed.add_field(
        name=f"❌ Denied ({len(denied)})",
        value="\n".join(denied) if denied else "None",
        inline=True
    )
    roles = [r.mention for r in target.roles if r.name != "@everyone"]
    embed.add_field(
        name=f"Roles ({len(roles)})",
        value=" ".join(roles) if roles else "None",
        inline=False
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="logs", description="Show recent server activity from the audit log")
@support_only()
async def logs(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    RELEVANT_ACTIONS = {
        discord.AuditLogAction.member_update: "Member Updated",
        discord.AuditLogAction.member_role_update: "Role Changed",
        discord.AuditLogAction.kick: "Member Kicked",
        discord.AuditLogAction.ban: "Member Banned",
        discord.AuditLogAction.unban: "Member Unbanned",
        discord.AuditLogAction.channel_create: "Channel Created",
        discord.AuditLogAction.channel_delete: "Channel Deleted",
        discord.AuditLogAction.message_delete: "Message Deleted",
    }

    try:
        entries = [
            entry async for entry in interaction.guild.audit_logs(limit=50)
            if entry.action in RELEVANT_ACTIONS
        ][:10]
    except discord.Forbidden:
        await interaction.followup.send(
            "❌ I don't have permission to view the audit log. Grant me **View Audit Log** permission.",
            ephemeral=True
        )
        return

    embed = discord.Embed(title="Recent Server Activity", color=discord.Color.blurple())
    if not entries:
        embed.description = "No recent relevant activity found."
    else:
        lines = []
        for e in entries:
            action = RELEVANT_ACTIONS[e.action]
            user = e.user.name if e.user else "Unknown"
            target = str(e.target) if e.target else "Unknown"
            ts = int(e.created_at.timestamp())
            lines.append(f"<t:{ts}:R> **{action}** by `{user}` → `{target}`")
        embed.description = "\n".join(lines)

    embed.set_footer(text="Showing up to 10 relevant audit log entries")
    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="sendapplication", description="Send the application panel to a channel")
@app_commands.describe(channel="Channel to send the application panel to")
async def sendapplication(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You need Administrator permission to use this.", ephemeral=True)
        return
    embed = discord.Embed(
        title=f"{VANTA_EMOJI_MARKUP}  VANTA  /  TEAM APPLICATIONS",
        description=(
            "**Help build the Vanta community.**\n"
            "Join the people who help users, improve the experience and support the Vanta ecosystem.\n\n"
            "**01  /  APPLY**\n"
            "Complete the private application form below.\n\n"
            "**02  /  REVIEW**\n"
            "Authorized staff review your answers.\n\n"
            "**03  /  DECISION**\n"
            "The team contacts you when a decision is made."
        ),
        color=0x3978F6,
    )
    embed.add_field(name="COMMUNITY", value="Keep the server welcoming, useful and well organized.", inline=False)
    embed.add_field(name="PRODUCT SUPPORT", value="Help users with Vanta Launcher and other Vanta projects.", inline=False)
    embed.add_field(name="EXPECTATIONS", value="Clear communication, reliability and a constructive attitude.", inline=False)
    embed.set_author(name=f"{VANTA_EMOJI_MARKUP}  VANTA  •  COMMUNITY OPERATIONS")
    embed.set_footer(text="Vanta Staff  •  Applications are reviewed on an ongoing basis")
    embed.timestamp = datetime.utcnow()
    await channel.send(embed=embed, view=ApplicationPanelView())
    await interaction.response.send_message(f"✅ Application panel sent to {channel.mention}.", ephemeral=True)


@tree.command(name="sendsupportpanel", description="Send the support ticket panel to a channel")
@support_only()
@app_commands.describe(channel="Channel to send the panel to (defaults to the current channel)")
async def sendsupportpanel(interaction: discord.Interaction, channel: discord.TextChannel = None):
    channel = channel or interaction.channel

    embed = discord.Embed(
        title=f"{VANTA_EMOJI_MARKUP}  VANTA  /  SUPPORT CENTER",
        description=(
            "Dedicated help for Vanta Launcher, account access, mod compatibility "
            "and the wider Vanta ecosystem.\n\n"
            "Choose a topic below to route your request, then open a private ticket. "
            "Your conversation stays in one place so the support team can follow it through."
        ),
        color=0x3978F6,
    )
    embed.set_author(name=f"{VANTA_EMOJI_MARKUP}  VANTA  •  CUSTOMER SUPPORT")
    embed.add_field(
        name="01  ·  OPEN A REQUEST",
        value="Select the closest topic and describe the issue. Include versions and error details when relevant.",
        inline=False,
    )
    embed.add_field(
        name="02  ·  TEAM REVIEW",
        value="Your private channel is visible only to you and authorized support staff. Staff can assign and prioritize requests.",
        inline=False,
    )
    embed.add_field(
        name="03  ·  RESOLUTION",
        value="Continue the conversation in your ticket. Close it when the issue is resolved.",
        inline=False,
    )
    embed.add_field(
        name="PRIVATE  /  TRACKED  /  PERSISTENT",
        value="Your ticket is private by default, recorded in the support queue and retained in the ticket system.",
        inline=False,
    )
    embed.set_footer(text="VANTA SUPPORT  •  Select a topic to get started")
    embed.timestamp = datetime.utcnow()

    await channel.send(embed=embed, view=SupportPanelView())
    await interaction.response.send_message(f"✅ Support panel sent to {channel.mention}!", ephemeral=True)

async def validate_mc_username(username: str):
    """
    Check Mojang's API to confirm an IGN exists and get its correct capitalisation.
    Returns (True, correct_ign) on success, (False, error_message) on failure.
    Handles every failure path: 404 (not found), non-200, timeout, network error,
    and bad JSON — so callers never need to try/except this.
    """
    if not re.fullmatch(r"[A-Za-z0-9_]{1,16}", username):
        return False, "Enter a valid Minecraft username (1–16 letters, numbers or underscores)."
    try:
        import aiohttp
        url = f"https://api.mojang.com/users/profiles/minecraft/{username}"
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    try:
                        data = await resp.json(content_type=None)
                        return True, data["name"]  # Mojang returns the correctly-cased name
                    except (KeyError, Exception):
                        # Malformed response — treat as unknown error, don't block the user
                        return True, username
                elif resp.status == 404:
                    return False, f"❌ `{username}` doesn't exist on Mojang. Check for typos."
                elif resp.status == 429:
                    # Rate limited — don't block the user, just let it through
                    return True, username
                else:
                    # Any other unexpected status — don't block the user
                    return True, username
    except asyncio.TimeoutError:
        # Mojang API slow/down — don't block the user
        return True, username
    except Exception:
        # Network error or anything else — don't block the user
        return True, username


@tree.command(name="link", description="Link your Minecraft account to your Discord")
@app_commands.describe(minecraft_username="Your Minecraft IGN")
async def link(interaction: discord.Interaction, minecraft_username: str):
    await interaction.response.defer(ephemeral=True)
    valid, result = await validate_mc_username(minecraft_username)
    if not valid:
        await interaction.followup.send(result, ephemeral=True)
        return
    minecraft_username = result
    discord_id = str(interaction.user.id)
    existing = await database.get_minecraft_link(discord_id)
    taken = await database.get_minecraft_link_by_username(minecraft_username)
    if taken and taken["discord_id"] != discord_id:
        await interaction.followup.send(f"❌ `{minecraft_username}` is already linked to another Discord account.", ephemeral=True)
        return
    try:
        await database.upsert_minecraft_link(discord_id, minecraft_username, datetime.utcnow().isoformat())
    except MinecraftLinkConflict:
        await interaction.followup.send(f"❌ `{minecraft_username}` is already linked to another Discord account.", ephemeral=True)
        return
    verb = "updated" if existing else "linked"
    await interaction.followup.send(f"✅ Successfully {verb} your Minecraft account: `{minecraft_username}`", ephemeral=True)
@tree.command(name="checklink", description="Check which Minecraft account is linked to a Discord user")
@app_commands.describe(user="The Discord user to check (leave empty to check yourself)")
async def checklink(interaction: discord.Interaction, user: discord.Member = None):
    target = user or interaction.user
    if user and user.id != interaction.user.id:
        if not await is_support_staff(interaction.user, interaction.guild):
            await interaction.response.send_message(
                "Only support staff can inspect another member's Minecraft link.", ephemeral=True
            )
            return
    row = await database.get_minecraft_link(str(target.id))
    if not row:
        msg = ("You haven't linked a Minecraft account yet. Use `/link` to link one."
               if not user else f"{target.mention} hasn't linked a Minecraft account.")
        await interaction.response.send_message(msg, ephemeral=True)
        return
    embed = discord.Embed(title="Minecraft Link", color=discord.Color.green())
    embed.add_field(name="Discord", value=target.mention, inline=True)
    embed.add_field(name="Minecraft IGN", value=f"`{row['mc_username']}`", inline=True)
    embed.add_field(name="Linked At", value=row["linked_at"][:10], inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)
@tree.command(name="forcelink", description="Manually link a Minecraft account to a Discord user (admin only)")
@app_commands.describe(user="The Discord user to link", minecraft_username="Their Minecraft IGN")
async def forcelink(interaction: discord.Interaction, user: discord.Member, minecraft_username: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You need Administrator permission to use this.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    valid, result = await validate_mc_username(minecraft_username)
    if not valid:
        await interaction.followup.send(result, ephemeral=True)
        return
    minecraft_username = result
    discord_id = str(user.id)
    existing = await database.get_minecraft_link(discord_id)
    taken = await database.get_minecraft_link_by_username(minecraft_username)
    if taken and taken["discord_id"] != discord_id:
        other = interaction.guild.get_member(int(taken["discord_id"]))
        other_mention = other.mention if other else f"`{taken['discord_id']}`"
        await interaction.followup.send(f"❌ `{minecraft_username}` is already linked to {other_mention}. Use `/forceunlink` first.", ephemeral=True)
        return
    try:
        await database.upsert_minecraft_link(discord_id, minecraft_username, datetime.utcnow().isoformat())
    except MinecraftLinkConflict:
        await interaction.followup.send(f"❌ `{minecraft_username}` is already linked to another Discord account.", ephemeral=True)
        return
    verb = "updated" if existing else "linked"
    old = f" (was `{existing['mc_username']}`)" if existing else ""
    await interaction.followup.send(f"✅ Successfully {verb} {user.mention}'s Minecraft account to `{minecraft_username}`{old}.", ephemeral=True)
@tree.command(name="forceunlink", description="Remove a user's linked Minecraft account (admin only)")
@app_commands.describe(user="The Discord user to unlink")
async def forceunlink(interaction: discord.Interaction, user: discord.Member):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You need Administrator permission to use this.", ephemeral=True)
        return
    row = await database.delete_minecraft_link(str(user.id))
    if not row:
        await interaction.response.send_message(f"{user.mention} doesn't have a Minecraft account linked.", ephemeral=True)
        return
    await interaction.response.send_message(f"✅ Unlinked `{row['mc_username']}` from {user.mention}.", ephemeral=True)




@tree.command(name="serverconfig", description="View or update this server's Vanta Bot configuration")
@app_commands.describe(
    support_category="Category where support tickets are created",
    support_role="Role allowed to manage support tickets",
    ticket_log_channel="Channel for ticket closure logs",
    application_channel="Channel receiving staff applications",
    launcher_channel="Channel for Vanta Launcher release announcements",
    launcher_role="Role pinged for Launcher releases",
)
@app_commands.checks.has_permissions(administrator=True)
async def serverconfig(
    interaction: discord.Interaction,
    support_category: discord.CategoryChannel = None,
    support_role: discord.Role = None,
    ticket_log_channel: discord.TextChannel = None,
    application_channel: discord.TextChannel = None,
    launcher_channel: discord.TextChannel = None,
    launcher_role: discord.Role = None,
):
    await interaction.response.defer(ephemeral=True)
    current = await database.get_guild_config(str(interaction.guild.id)) or {}
    supplied = {
        "support_category_id": support_category.id if support_category else None,
        "support_role_id": support_role.id if support_role else None,
        "ticket_log_channel_id": ticket_log_channel.id if ticket_log_channel else None,
        "application_result_channel_id": application_channel.id if application_channel else None,
        "launcher_announcement_channel_id": launcher_channel.id if launcher_channel else None,
        "launcher_announcement_role_id": launcher_role.id if launcher_role else None,
    }
    changed = {key: str(value) for key, value in supplied.items() if value is not None}
    if changed:
        merged = {key: current.get(key) for key in supplied}
        merged.update(changed)
        await database.update_guild_config(
            str(interaction.guild.id), updated_by=str(interaction.user.id),
            updated_at=datetime.utcnow().isoformat(), **merged,
        )
        await record_audit(interaction.guild.id, interaction.user.id, "guild.config.updated",
                           details={"fields": sorted(changed.keys())})
        current = await database.get_guild_config(str(interaction.guild.id)) or {}
    labels = [
        ("Support category", "support_category_id"),
        ("Support role", "support_role_id"),
        ("Ticket log channel", "ticket_log_channel_id"),
        ("Application channel", "application_result_channel_id"),
        ("Launcher release channel", "launcher_announcement_channel_id"),
        ("Launcher announcement role", "launcher_announcement_role_id"),
    ]
    lines = []
    for label, key in labels:
        raw = current.get(key)
        rendered = f"<@&{raw}>" if key.endswith("role_id") and raw else f"<#{raw}>" if raw else "Not configured (legacy default may apply)"
        lines.append(f"**{label}:** {rendered}")
    embed = discord.Embed(title="VANTA  /  SERVER CONFIGURATION",
                          description="\n".join(lines), color=0x3978F6)
    embed.set_footer(text="Only server administrators can view or change this configuration")
    await interaction.followup.send(embed=embed, ephemeral=True)



@tree.command(name="botdashboard", description="Open the Vanta Bot operations dashboard")
@support_only()
async def botdashboard(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    database_ok = True
    try:
        await database.healthcheck()
        ticket_stats, link_stats = await asyncio.gather(
            database.get_support_ticket_stats(str(interaction.guild.id)),
            database.get_minecraft_link_stats(),
        )
    except Exception:
        database_ok = False
        ticket_stats = {"total": 0, "open": 0, "closed": 0}
        link_stats = {"total": 0}
    embed = discord.Embed(
        title="VANTA  /  OPERATIONS",
        description="Live operational snapshot for this Discord server.",
        color=0x3978F6 if database_ok else 0xD83C3E,
    )
    embed.add_field(
        name="Platform status",
        value=f"**Discord:** Connected\n**D1:** {'Healthy' if database_ok else 'Degraded'}\n**Gateway latency:** {round(client.latency * 1000)} ms",
        inline=True,
    )
    embed.add_field(
        name="Support operations",
        value=f"**Total:** {ticket_stats['total']:,}\n**Open:** {ticket_stats['open']:,}\n**Closed:** {ticket_stats['closed']:,}",
        inline=True,
    )
    embed.add_field(
        name="Minecraft links",
        value=f"**Registered:** {int(link_stats.get('total') or 0):,}",
        inline=True,
    )
    uptime = datetime.utcnow() - BOT_STARTED_AT
    hours, remainder = divmod(int(uptime.total_seconds()), 3600)
    minutes = remainder // 60
    embed.add_field(name="Runtime", value=f"**Uptime:** {hours}h {minutes}m\n**Guild:** {discord.utils.escape_markdown(interaction.guild.name)}", inline=False)
    embed.set_author(name="VANTA  •  PLATFORM CONTROL")
    embed.set_footer(text=f"Requested by {interaction.user} · Metrics queried live from D1")
    embed.timestamp = datetime.utcnow()
    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="auditlog", description="View recent Vanta Bot operational audit events")
@app_commands.describe(limit="Number of events to display (1–15)")
@support_only()
async def auditlog(interaction: discord.Interaction, limit: app_commands.Range[int, 1, 15] = 10):
    await interaction.response.defer(ephemeral=True)
    events = await database.list_audit_events(str(interaction.guild.id), limit=limit)
    embed = discord.Embed(title="VANTA  /  AUDIT TRAIL", color=0x3978F6)
    if not events:
        embed.description = "No recorded operational events yet."
    else:
        lines = []
        for event in events:
            timestamp = int(datetime.fromisoformat(event["created_at"].replace("Z", "+00:00")).timestamp())
            target = f" → <@{event['target_id']}>" if event.get("target_id") else ""
            lines.append(f"<t:{timestamp}:R> **{discord.utils.escape_markdown(event['action'])}** · <@{event['actor_id']}>{target}")
        embed.description = "\n".join(lines)
    embed.set_footer(text="Append-only operational events · Cloudflare D1")
    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="ticketassign", description="Assign an open support ticket to a staff member")
@app_commands.describe(channel="The support ticket channel", staff="Support staff member taking ownership")
@support_only()
async def ticketassign(interaction: discord.Interaction, channel: discord.TextChannel, staff: discord.Member):
    if not await is_support_staff(staff, interaction.guild):
        await interaction.response.send_message("The assignee must be a support staff member or administrator.", ephemeral=True)
        return
    if not channel.name.startswith("support-"):
        await interaction.response.send_message("Select an active support ticket channel.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        ticket = await database.assign_support_ticket(
            str(channel.id), str(staff.id), str(interaction.user.id), datetime.utcnow().isoformat()
        )
    except DatabaseError as exc:
        await interaction.followup.send(f"Could not assign ticket: {exc}", ephemeral=True)
        return
    embed = discord.Embed(title="Ticket Assigned", description=f"This ticket is now assigned to {staff.mention}.", color=discord.Color.blurple())
    embed.set_footer(text=f"Assigned by {interaction.user} · {ticket['assigned_at'][:10]}")
    await channel.send(embed=embed)
    await record_audit(interaction.guild.id, interaction.user.id, "ticket.assigned", target_id=channel.id, details={"assignee_id": str(staff.id)})
    await interaction.followup.send(f"Assigned {channel.mention} to {staff.mention}.", ephemeral=True)


@tree.command(name="ticketstats", description="Show persistent support ticket statistics")
@support_only()
async def ticketstats(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    stats = await database.get_support_ticket_stats(str(interaction.guild.id))
    embed = discord.Embed(title="Support Operations", color=discord.Color.blurple())
    embed.add_field(name="Total tickets", value=str(stats["total"]), inline=True)
    embed.add_field(name="Open", value=str(stats["open"]), inline=True)
    embed.add_field(name="Closed", value=str(stats["closed"]), inline=True)
    embed.set_footer(text="Persistent records stored in Cloudflare D1")
    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="mytickets", description="View your recent support ticket history")
async def mytickets(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    tickets, total = await database.list_support_tickets(
        str(interaction.guild.id), owner_id=str(interaction.user.id), status="all", limit=10
    )
    if not tickets:
        await interaction.followup.send("You have no recorded support tickets in this server.", ephemeral=True)
        return
    lines = []
    for ticket in tickets:
        state = "Open" if ticket["status"] == "open" else "Closed"
        channel = interaction.guild.get_channel(int(ticket["channel_id"]))
        channel_text = channel.mention if channel else "Channel unavailable"
        lines.append(f"{state} - {channel_text} - {ticket['created_at'][:10]}")
    embed = discord.Embed(title="Your Support History", description="\n".join(lines), color=discord.Color.blurple())
    embed.set_footer(text=f"{total} recorded ticket(s) - showing latest 10")
    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="linkstats", description="Show Minecraft account-linking statistics")
async def linkstats(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    stats = await database.get_minecraft_link_stats()
    total = int(stats.get("total") or 0)
    embed = discord.Embed(title="Minecraft Linking Statistics", color=discord.Color.blurple())
    embed.add_field(name="Linked accounts", value=f"`{total:,}`", inline=True)
    if stats.get("first_linked_at"):
        embed.add_field(name="First link", value=stats["first_linked_at"][:10], inline=True)
    if stats.get("latest_linked_at"):
        embed.add_field(name="Latest link", value=stats["latest_linked_at"][:10], inline=True)
    embed.set_footer(text="Live counts from Cloudflare D1")
    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="linkedusers", description="Browse registered Minecraft links (support staff)")
@app_commands.describe(page="Page number (25 accounts per page)")
@support_only()
async def linkedusers(interaction: discord.Interaction, page: app_commands.Range[int, 1, 4000] = 1):
    await interaction.response.defer(ephemeral=True)
    limit = 25
    offset = (page - 1) * limit
    rows, total = await database.list_minecraft_links(limit=limit, offset=offset)
    if not rows:
        await interaction.followup.send(
            f"No linked accounts on page {page}. There are {total:,} linked accounts in total.",
            ephemeral=True
        )
        return
    lines = []
    for row in rows:
        member = interaction.guild.get_member(int(row["discord_id"]))
        discord_label = member.mention if member else f"`{row['discord_id']}`"
        lines.append(f"{discord_label} — **{discord.utils.escape_markdown(row['mc_username'])}**")
    pages = max(1, (total + limit - 1) // limit)
    embed = discord.Embed(
        title=f"Minecraft Accounts · Page {page}/{pages}",
        description="\n".join(lines),
        color=discord.Color.blurple()
    )
    embed.set_footer(text=f"{total:,} total linked accounts · Newest links first")
    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="lookupmc", description="Find the Discord account linked to a Minecraft username")
@app_commands.describe(minecraft_username="Minecraft username to look up")
@support_only()
async def lookupmc(interaction: discord.Interaction, minecraft_username: str):
    if not re.fullmatch(r"[A-Za-z0-9_]{1,16}", minecraft_username):
        await interaction.response.send_message("Enter a valid Minecraft username (1–16 letters, numbers or underscores).", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    row = await database.get_minecraft_link_by_username(minecraft_username)
    if not row:
        await interaction.followup.send(f"No Discord account is linked to `{discord.utils.escape_markdown(minecraft_username)}`.", ephemeral=True)
        return
    member = interaction.guild.get_member(int(row["discord_id"]))
    owner = member.mention if member else f"`{row['discord_id']}` (not currently in this server)"
    embed = discord.Embed(title="Minecraft Account Lookup", color=discord.Color.green())
    embed.add_field(name="Minecraft username", value=f"`{discord.utils.escape_markdown(row['mc_username'])}`", inline=True)
    embed.add_field(name="Linked Discord account", value=owner, inline=True)
    embed.add_field(name="Linked on", value=row["linked_at"][:10], inline=True)
    await interaction.followup.send(embed=embed, ephemeral=True)


# ── HTTP health endpoint ──────────────────────────────────────────────────────

async def health(request):
    return web.json_response({"status": "ok", "service": "vanta-discord-bot"})


async def api_health(request):
    return web.json_response({"status": "ok", "service": "vanta-discord-bot"})


async def main():
    token = settings.require_discord_token()
    await init_db()
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/api", api_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT, reuse_port=True)
    await site.start()
    print(f"Health check running on port {PORT}")
    try:
        await client.start(token)
    finally:
        await runner.cleanup()
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())












