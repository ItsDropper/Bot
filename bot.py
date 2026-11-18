import os
import asyncio
import re
from datetime import datetime
import discord
from discord import app_commands
from aiohttp import web

from core.database import CloudflareDatabase, DatabaseError, MinecraftLinkConflict

PORT = int(os.environ.get("PORT", 3001))

TICKET_CATEGORY_ID = 1514689354281259170
SUPPORT_ROLE_ID = 1514274160329756673
TICKET_LOG_CHANNEL_ID = 1514004581410013264
LAUNCHER_ANNOUNCEMENT_CHANNEL_ID = 1530652249653645422
LAUNCHER_ANNOUNCEMENT_ROLE_ID = 1530664744929722418
LAUNCHER_RELEASE_TAG = "v1.3.2"
LAUNCHER_RELEASE_URL = "https://github.com/ItsDropper/Vanta/releases/tag/v1.3.2"

intents = discord.Intents.default()

# ── Cloudflare D1 ─────────────────────────────────────────────────────────────

database: CloudflareDatabase | None = None

async def init_db():
    global database
    worker_url = os.environ.get("CLOUDFLARE_WORKER_URL", "").strip()
    api_token = os.environ.get("CLOUDFLARE_BOT_API_TOKEN", "").strip()
    if not worker_url or not api_token:
        raise RuntimeError("CLOUDFLARE_WORKER_URL and CLOUDFLARE_BOT_API_TOKEN are required")
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


async def close_db():
    global database
    if database is not None:
        await database.close()
        database = None


# ── Permission checks ────────────────────────────────────────────────────────

# ── Permission check ──────────────────────────────────────────────────────────

def is_support_staff(member: discord.Member, guild: discord.Guild) -> bool:
    support_role = guild.get_role(SUPPORT_ROLE_ID)
    return member.guild_permissions.administrator or bool(support_role and support_role in member.roles)


async def require_support_staff(interaction: discord.Interaction) -> bool:
    if is_support_staff(interaction.user, interaction.guild):
        return True
    message = "Only support staff or administrators can use this control."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)
    return False


def support_only():
    async def predicate(interaction: discord.Interaction):
        support_role = interaction.guild.get_role(SUPPORT_ROLE_ID)
        has_role = support_role and support_role in interaction.user.roles
        is_admin = interaction.user.guild_permissions.administrator
        if has_role or is_admin:
            return True
        await interaction.response.send_message(
            "You need the **support** role to use this command.", ephemeral=True
        )
        return False
    return app_commands.check(predicate)

# ── Support ticket close control ──────────────────────────────────────────────

class CloseSupportTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _close(self, interaction: discord.Interaction):
        topic = interaction.channel.topic or ""
        owner_match = re.fullmatch(r"vanta_ticket_owner:([0-9]{15,22})", topic)
        is_owner = bool(owner_match and int(owner_match.group(1)) == interaction.user.id)
        if not is_owner and not is_support_staff(interaction.user, interaction.guild):
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
        if TICKET_LOG_CHANNEL_ID:
            log_channel = interaction.guild.get_channel(TICKET_LOG_CHANNEL_ID)
            if log_channel:
                embed = discord.Embed(title="Ticket Closed", color=discord.Color.red())
                embed.add_field(name="Channel", value=interaction.channel.name, inline=True)
                embed.add_field(name="Closed by", value=interaction.user.mention, inline=True)
                embed.set_footer(text=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))
                await log_channel.send(embed=embed)
        await interaction.followup.send('Ticket closed and recorded in the database.', ephemeral=True)
        await interaction.channel.delete()

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger,
                       custom_id="support_ticket_close", emoji="🔒")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._close(interaction)



# ── Support ticket system ─────────────────────────────────────────────────────

class SupportTicketModal(discord.ui.Modal, title="Create Support Ticket"):
    reason = discord.ui.TextInput(
        label="Why are you creating this ticket?",
        style=discord.TextStyle.paragraph,
        placeholder="Describe your issue or question...",
        max_length=1000
    )

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        category = guild.get_channel(TICKET_CATEGORY_ID)

        # Anti-duplicate: block if user already has an open support ticket
        if category:
            for ch in category.channels:
                if ch.name.startswith("support-"):
                    ow = ch.overwrites_for(interaction.user)
                    if ow.view_channel is True:
                        await interaction.response.send_message(
                            f"You already have an open support ticket: {ch.mention}", ephemeral=True
                        )
                        return

        support_role = guild.get_role(SUPPORT_ROLE_ID)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        }

        if support_role:
            overwrites[support_role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True
            )

        safe_name = interaction.user.name.lower().replace(" ", "-")[:20]

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
                reason=self.reason.value,
                created_at=datetime.utcnow().isoformat(),
            )
        except DatabaseError as exc:
            await channel.delete(reason="Ticket database registration failed")
            await interaction.followup.send(
                f"Ticket could not be registered. No ticket was left open. {exc}", ephemeral=True
            )
            return

        embed = discord.Embed(title="🎫 Support Ticket", color=discord.Color.blurple())
        embed.add_field(name="Discord", value=interaction.user.mention, inline=True)
        embed.add_field(name="Reason", value=self.reason.value, inline=False)
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.set_footer(text="Opened at")
        embed.timestamp = datetime.utcnow()

        ping = support_role.mention if support_role else "@support"
        await channel.send(content=ping, embed=embed, view=CloseSupportTicketView())
        await interaction.followup.send(
            f"Ticket created: {channel.mention}", ephemeral=True
        )


class SupportPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Create Support Ticket",
        style=discord.ButtonStyle.success,
        custom_id="support_ticket_open",
        emoji="🎫"
    )
    async def open_support_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SupportTicketModal())



APPLICATION_RESULT_CHANNEL_ID = 1514193984094732383

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
        channel = interaction.guild.get_channel(APPLICATION_RESULT_CHANNEL_ID)
        if not channel:
            await interaction.response.send_message("Application channel not found. Contact an admin.", ephemeral=True)
            return
        embed = discord.Embed(title="🛡️ New Staff Application", color=0xFFAA00)
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
        category = interaction.guild.get_channel(TICKET_CATEGORY_ID)
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

    @discord.ui.button(label="Apply — Vanta Staff", style=discord.ButtonStyle.primary,
                       custom_id="app_open_staff", emoji="🛡️")
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

class Client(discord.Client):
    async def setup_hook(self):
        self.add_view(CloseSupportTicketView())
        self.add_view(SupportPanelView())
        self.add_view(ApplicationPanelView())
        self.add_view(LegacyTesterApplicationView())
        self.add_view(LegacyTierPanelView())
        self.add_view(ApplicationResultView())

    async def on_ready(self):
        for guild in self.guilds:
            tree.copy_global_to(guild=guild)
            await tree.sync(guild=guild)
            await reconcile_ticket_close_controls(guild)
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        print(f"Commands synced to {len(self.guilds)} guild(s).")

async def reconcile_ticket_close_controls(guild: discord.Guild):
    """Replace old ticket messages that exposed both close controls with one control."""
    category = guild.get_channel(TICKET_CATEGORY_ID)
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

@client.event
async def on_guild_join(guild):
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)
    print(f"Synced commands to {guild.name}")

@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    # Unwrap CheckFailure — permission checks already send their own message
    if isinstance(error, app_commands.CheckFailure):
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
    channel = interaction.guild.get_channel(LAUNCHER_ANNOUNCEMENT_CHANNEL_ID)
    if channel is None:
        try:
            channel = await interaction.guild.fetch_channel(LAUNCHER_ANNOUNCEMENT_CHANNEL_ID)
        except (discord.NotFound, discord.Forbidden):
            await interaction.followup.send("The configured launcher announcement channel is unavailable.", ephemeral=True)
            return
    if not isinstance(channel, discord.TextChannel):
        await interaction.followup.send("The configured launcher announcement destination is not a text channel.", ephemeral=True)
        return

    role = interaction.guild.get_role(LAUNCHER_ANNOUNCEMENT_ROLE_ID)
    if role is None:
        await interaction.followup.send("The configured launcher announcement role was not found.", ephemeral=True)
        return

    embed = discord.Embed(
        title="Vanta Launcher Update — v1.3.2",
        description=(
            "A new Vanta Launcher release is available. Update to the latest version "
            "to get the newest launcher improvements and fixes.\n\n"
            f"[View release notes and downloads]({LAUNCHER_RELEASE_URL})"
        ),
        url=LAUNCHER_RELEASE_URL,
        color=0x3978F6,
    )
    embed.set_footer(text="Vanta Launcher • Official Release")
    embed.timestamp = datetime.now().astimezone()
    try:
        message = await channel.send(
            content=role.mention,
            embed=embed,
            allowed_mentions=discord.AllowedMentions(
                roles=[role], users=False, everyone=False, replied_user=False
            ),
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "I cannot post there. Check View Channel, Send Messages, Embed Links, and role mention permissions.",
            ephemeral=True,
        )
        return
    await interaction.followup.send(
        f"Launcher update announcement posted in {channel.mention} (message {message.id}).",
        ephemeral=True,
    )


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
        title="Join the Vanta Team",
        description=(
            "Want to help improve the Vanta community? Apply to join the staff team.\n\n"
            "Staff help moderate the community, support Launcher and Client users, "
            "and keep the server organized.\n\n"
            "Applications are reviewed by the server administrators."
        ),
        color=0x3978F6
    )
    embed.set_image(url="https://i.imgur.com/MV0Xqr5.png")
    embed.set_footer(text="Vanta • Staff Applications")
    await channel.send(embed=embed, view=ApplicationPanelView())
    await interaction.response.send_message(f"✅ Application panel sent to {channel.mention}.", ephemeral=True)


@tree.command(name="sendsupportpanel", description="Send the support ticket panel to a channel")
@support_only()
@app_commands.describe(channel="Channel to send the panel to (defaults to the current channel)")
async def sendsupportpanel(interaction: discord.Interaction, channel: discord.TextChannel = None):
    channel = channel or interaction.channel

    embed = discord.Embed(
        title="🎫  Support",
        description=(
            "Need help or have a question? Open a support ticket below and our "
            "support team will get to you shortly.\n\n"
            "**How it works:**\n"
            "1️⃣  Click **Create Support Ticket** below\n"
            "2️⃣  Tell us why you're opening a ticket\n"
            "3️⃣  A member of support will join your ticket shortly"
        ),
        color=0x2B2D31
    )

    embed.set_footer(text="Open a ticket to get started")

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
        support_role = interaction.guild.get_role(SUPPORT_ROLE_ID)
        if not interaction.user.guild_permissions.administrator and (not support_role or support_role not in interaction.user.roles):
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



@tree.command(name="ticketassign", description="Assign an open support ticket to a staff member")
@app_commands.describe(channel="The support ticket channel", staff="Support staff member taking ownership")
@support_only()
async def ticketassign(interaction: discord.Interaction, channel: discord.TextChannel, staff: discord.Member):
    support_role = interaction.guild.get_role(SUPPORT_ROLE_ID)
    if not staff.guild_permissions.administrator and (not support_role or support_role not in staff.roles):
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
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN environment variable is required")
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












