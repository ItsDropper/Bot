import os
import asyncio
import sqlite3
from datetime import datetime, timedelta
import discord
from discord import app_commands
from aiohttp import web

TOKEN = os.environ["DISCORD_BOT_TOKEN"]
PORT = int(os.environ.get("PORT", 3001))
DB_PATH = "tiers.db"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = "Itsdropper/bot"
GITHUB_FILE_PATH = "tiers.db"

async def backup_db_to_github():
    try:
        import base64
        import aiohttp
        with open(DB_PATH, "rb") as f:
            content = base64.b64encode(f.read()).decode()
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}"
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json"
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                sha = None
                if resp.status == 200:
                    data = await resp.json()
                    sha = data.get("sha")
            payload = {"message": "Auto-backup tiers.db", "content": content}
            if sha:
                payload["sha"] = sha
            async with session.put(url, headers=headers, json=payload) as resp:
                if resp.status not in (200, 201):
                    print(f"GitHub backup failed: {resp.status}")
    except Exception as e:
        print(f"GitHub backup error: {e}")

async def restore_db_from_github():
    if os.path.exists(DB_PATH):
        print("Database already exists, skipping restore.")
        return
    try:
        import base64
        import aiohttp
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}"
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json"
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    content = base64.b64decode(data["content"])
                    with open(DB_PATH, "wb") as f:
                        f.write(content)
                    print("Database restored from GitHub!")
                else:
                    print(f"No backup found on GitHub: {resp.status}")
    except Exception as e:
        print(f"GitHub restore error: {e}")

PANEL_CHANNEL_ID = 1517249587931250760
TICKET_CATEGORY_ID = 1514689354281259170
TESTER_ROLE_ID = 1514260245034041485
TICKET_LOG_CHANNEL_ID = 0  # Set to your staff log channel ID to enable ticket close logging

intents = discord.Intents.default()

# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL mode allows concurrent readers to always see the latest committed write,
    # which fixes the API returning stale data after /tier is used.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                discord_id TEXT PRIMARY KEY,
                username   TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tier_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    TEXT NOT NULL,
                gamemode   TEXT NOT NULL,
                tier       TEXT NOT NULL,
                tester_id  TEXT NOT NULL,
                timestamp  TEXT NOT NULL,
                notes      TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS minecraft_links (
                discord_id  TEXT PRIMARY KEY,
                mc_username TEXT NOT NULL UNIQUE,
                linked_at   TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS player_notes (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    TEXT NOT NULL,
                author_id  TEXT NOT NULL,
                note       TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.commit()

def ensure_user(discord_id: str, username: str):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO users (discord_id, username) VALUES (?, ?)"
            " ON CONFLICT(discord_id) DO UPDATE SET username=excluded.username",
            (discord_id, username)
        )
        conn.commit()

def insert_tier_record(user_id: str, gamemode: str, tier: str, tester_id: str, notes: str = None):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO tier_history (user_id, gamemode, tier, tester_id, timestamp, notes)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, gamemode, tier, tester_id, datetime.utcnow().isoformat(), notes)
        )
        conn.commit()
    # Safely schedule the async backup from this sync function by grabbing the
    # running event loop. asyncio.create_task() alone can silently fail here.
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(backup_db_to_github())
    except Exception as e:
        print(f"Failed to schedule GitHub backup: {e}")

def get_cooldown_expiry(discord_id: str, gamemode: str):
    """Returns expiry datetime if user is on cooldown for gamemode, else None."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT timestamp FROM tier_history WHERE user_id = ? AND gamemode = ?"
            " ORDER BY timestamp DESC LIMIT 1",
            (discord_id, gamemode)
        ).fetchone()
    if not row:
        return None
    last_tiered = datetime.fromisoformat(row["timestamp"])
    expiry = last_tiered + timedelta(days=30)
    return expiry if datetime.utcnow() < expiry else None

def fetch_user_history(discord_id: str, limit: int = 10):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM tier_history WHERE user_id = ?"
            " ORDER BY timestamp DESC LIMIT ?",
            (discord_id, limit)
        ).fetchall()
    return rows

def fetch_leaderboard(gamemode: str):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT th.user_id, u.username, th.tier, th.timestamp
            FROM tier_history th
            LEFT JOIN users u ON u.discord_id = th.user_id
            WHERE th.gamemode = ?
              AND th.id = (
                  SELECT id FROM tier_history
                  WHERE user_id = th.user_id AND gamemode = th.gamemode
                  ORDER BY timestamp DESC LIMIT 1
              )
            ORDER BY th.tier DESC
            """,
            (gamemode,)
        ).fetchall()
    return rows

# ── Constants ─────────────────────────────────────────────────────────────────

GAMEMODES = ["sword", "axe", "mace", "uhc", "netheriteop", "pot", "smp", "crystal", "cart pvp"]

TIERS = ["LT5", "HT5", "LT4", "HT4", "LT3", "HT3", "LT2", "HT2", "LT1", "HT1"]

TIER_OPTIONS = ["No Tier"] + TIERS

RESULT_CHANNELS = {
    "sword": 1514012563279708222,
    "axe": 1514260878382469240,
    "mace": 1514011633423224902,
    "uhc": 1514012633244893355,
    "netheriteop": 1514263526305173565,
    "pot": 1514259434568683701,
    "smp": 1514260813932662835,
    "crystal": 1514001478531153931,
    "cart pvp": 1514303767523098724,
}

# ── Permission check ──────────────────────────────────────────────────────────

def tester_only():
    async def predicate(interaction: discord.Interaction):
        tester_role = interaction.guild.get_role(TESTER_ROLE_ID)
        has_role = tester_role and tester_role in interaction.user.roles
        is_admin = interaction.user.guild_permissions.administrator
        if has_role or is_admin:
            return True
        await interaction.response.send_message(
            "You need the **@tester** role to use this command.", ephemeral=True
        )
        return False
    return app_commands.check(predicate)

# ── Ticket UI ─────────────────────────────────────────────────────────────────

class TicketModal(discord.ui.Modal, title="Tier Test Application"):
    ign = discord.ui.TextInput(
        label="Minecraft Username",
        placeholder="Your IGN...",
        max_length=64
    )
    server = discord.ui.TextInput(
        label="Preferred PvP Server",
        placeholder="e.g. mcpcp.club, flowpvp.gg, minemen.club...",
        max_length=64
    )

    def __init__(self, gamemode: str, current_tier: str):
        super().__init__()
        self.gamemode = gamemode
        self.current_tier = current_tier

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        category = guild.get_channel(TICKET_CATEGORY_ID)

        # Cooldown check: block if tiered in this gamemode within the last 30 days
        expiry = get_cooldown_expiry(str(interaction.user.id), self.gamemode)
        if expiry:
            ts = int(expiry.timestamp())
            await interaction.response.send_message(
                f"❌ You're on cooldown for **{self.gamemode.upper()}**.\n"
                f"You can open a ticket again <t:{ts}:R> (on <t:{ts}:D>).",
                ephemeral=True
            )
            return

        # Anti-duplicate: block if user already has an open ticket
        if category:
            for ch in category.channels:
                ow = ch.overwrites_for(interaction.user)
                if ow.view_channel is True:
                    await interaction.response.send_message(
                        f"You already have an open ticket: {ch.mention}", ephemeral=True
                    )
                    return

        tester_role = guild.get_role(TESTER_ROLE_ID)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        }
        if tester_role:
            overwrites[tester_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        safe_name = interaction.user.name.lower().replace(" ", "-")[:20]
        safe_gamemode = self.gamemode.lower().replace(" ", "-")
        channel = await guild.create_text_channel(
            name=f"ticket-{safe_gamemode}-{safe_name}",
            category=category,
            overwrites=overwrites
        )

        embed = discord.Embed(title="Tier Test Ticket", color=discord.Color.green())
        embed.add_field(name="Discord", value=interaction.user.mention, inline=True)
        embed.add_field(name="Minecraft Username", value=self.ign.value, inline=True)
        embed.add_field(name="Gamemode", value=self.gamemode.upper(), inline=True)
        embed.add_field(name="Current Tier", value=self.current_tier, inline=True)
        embed.add_field(name="Preferred PvP Server", value=self.server.value, inline=True)

        ping = tester_role.mention if tester_role else "@tester"
        await channel.send(content=ping, embed=embed, view=CloseTicketView())
        await interaction.response.send_message(
            f"Ticket created: {channel.mention}", ephemeral=True
        )


class TierSelectView(discord.ui.View):
    def __init__(self, gamemode: str):
        super().__init__(timeout=120)
        self.gamemode = gamemode

    @discord.ui.select(
        placeholder="Select your current tier...",
        options=[discord.SelectOption(label=t, value=t) for t in TIER_OPTIONS]
    )
    async def tier_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.send_modal(TicketModal(self.gamemode, select.values[0]))


class GamemodeSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.select(
        placeholder="Select a gamemode...",
        options=[discord.SelectOption(label=g.upper(), value=g) for g in GAMEMODES]
    )
    async def gamemode_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        gm = select.values[0]
        await interaction.response.edit_message(
            content="Now select your current tier:", view=TierSelectView(gm)
        )


class TierTestView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Tier Test",
        style=discord.ButtonStyle.primary,
        custom_id="tier_test_open",
        emoji="⚔️"
    )
    async def tier_test(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Select a gamemode:", view=GamemodeSelectView(), ephemeral=True
        )

    @discord.ui.button(
        label="Check Cooldown",
        style=discord.ButtonStyle.secondary,
        custom_id="tier_check_cooldown",
        emoji="⏳"
    )
    async def check_cooldown(self, interaction: discord.Interaction, button: discord.ui.Button):
        discord_id = str(interaction.user.id)
        lines = []
        for gm in GAMEMODES:
            expiry = get_cooldown_expiry(discord_id, gm)
            if expiry:
                ts = int(expiry.timestamp())
                lines.append(f"**{gm.upper()}** — ❌ cooldown expires <t:{ts}:R>")
            else:
                lines.append(f"**{gm.upper()}** — ✅ ready")

        embed = discord.Embed(title="Your Tier Test Cooldowns", color=discord.Color.blurple())
        embed.description = "\n".join(lines)
        embed.set_footer(text="Cooldowns last 30 days from your last tier test.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Close Ticket",
        style=discord.ButtonStyle.danger,
        custom_id="tier_ticket_close",
        emoji="🔒"
    )
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Closing ticket...", ephemeral=True)
        if TICKET_LOG_CHANNEL_ID:
            log_ch = interaction.guild.get_channel(TICKET_LOG_CHANNEL_ID)
            if log_ch:
                log_embed = discord.Embed(title="🔒 Ticket Closed", color=discord.Color.red())
                log_embed.add_field(name="Channel", value=interaction.channel.name, inline=True)
                log_embed.add_field(name="Closed by", value=interaction.user.mention, inline=True)
                log_embed.set_footer(text=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))
                await log_ch.send(embed=log_embed)
        await interaction.channel.delete()

# ── Application system ────────────────────────────────────────────────────────

APPLICATION_RESULT_CHANNEL_ID = 1514193984094732383

class TesterApplicationModal(discord.ui.Modal, title="Tester Application"):
    ign = discord.ui.TextInput(
        label="Minecraft IGN",
        placeholder="Your exact in-game name",
        max_length=50
    )
    gamemodes = discord.ui.TextInput(
        label="Gamemodes (optional — blank = general)",
        placeholder="e.g. sword, axe, pot",
        required=False,
        max_length=200
    )
    reason = discord.ui.TextInput(
        label="Why do you want to become a tester?",
        style=discord.TextStyle.paragraph,
        placeholder="Be specific and detailed.",
        max_length=1000
    )

    async def on_submit(self, interaction: discord.Interaction):
        channel = interaction.guild.get_channel(APPLICATION_RESULT_CHANNEL_ID)
        if not channel:
            await interaction.response.send_message("Application channel not found. Contact an admin.", ephemeral=True)
            return
        embed = discord.Embed(title="🧪 New Tester Application", color=discord.Color.blue())
        embed.add_field(name="Applicant", value=f"{interaction.user.mention} (`{interaction.user.name}`)", inline=False)
        embed.add_field(name="Minecraft IGN", value=self.ign.value, inline=True)
        embed.add_field(name="Gamemodes", value=self.gamemodes.value or "Not specified", inline=True)
        embed.add_field(name="Why do they want to be a tester?", value=self.reason.value, inline=False)
        embed.set_footer(text=f"Application type: tester | User ID: {interaction.user.id}")
        embed.timestamp = datetime.utcnow()
        await channel.send(embed=embed, view=ApplicationResultView())
        await interaction.response.send_message(
            "✅ Your tester application has been submitted! You'll be notified of the decision.", ephemeral=True
        )


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
        embed = discord.Embed(title="🛡️ New Staff Application", color=discord.Color.gold())
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
        user_id, app_type = self._parse(interaction.message)
        if not user_id:
            await interaction.response.send_message("Could not read applicant info from embed.", ephemeral=True)
            return
        try:
            member = interaction.guild.get_member(int(user_id))
            if not member:
                member = await interaction.guild.fetch_member(int(user_id))
            if member and app_type == "tester":
                tester_role = interaction.guild.get_role(TESTER_ROLE_ID)
                if tester_role:
                    await member.add_roles(tester_role)
            try:
                applicant = await client.fetch_user(int(user_id))
                dm_embed = discord.Embed(
                    title="🎉 Application Approved!",
                    description=f"Your **{app_type}** application in **{interaction.guild.name}** has been **approved**!",
                    color=discord.Color.green()
                )
                if app_type == "tester":
                    dm_embed.add_field(name="Next Steps", value="You now have the **Tester** role. Welcome to the team!", inline=False)
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
        tester_role = interaction.guild.get_role(TESTER_ROLE_ID)
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            interaction.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        }
        if tester_role:
            overwrites[tester_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        safe_name = member.name.lower().replace(" ", "-")[:20]
        channel = await interaction.guild.create_text_channel(
            name=f"app-{safe_name}",
            category=category,
            overwrites=overwrites
        )
        embed = discord.Embed(
            title="Application Discussion",
            description=f"Opened to discuss {member.mention}'s **{app_type}** application.",
            color=discord.Color.blurple()
        )
        await channel.send(content=f"{member.mention} {interaction.user.mention}", embed=embed, view=CloseTicketView())
        await interaction.response.send_message(f"💬 Private ticket created: {channel.mention}", ephemeral=True)

    @discord.ui.button(label="Deny with Reason", style=discord.ButtonStyle.danger, custom_id="app_result_deny_reason", emoji="📝")
    async def deny_with_reason(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id, app_type = self._parse(interaction.message)
        if not user_id:
            await interaction.response.send_message("Could not read applicant info from embed.", ephemeral=True)
            return
        await interaction.response.send_modal(DenyReasonModal(user_id, app_type, interaction.message))


class TesterWarningView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(
        label="I Understand — Apply Now",
        style=discord.ButtonStyle.danger,
        emoji="⚔️"
    )
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.send_modal(TesterApplicationModal())
        except Exception as e:
            await interaction.response.send_message(
                f"Error: {e}",
                ephemeral=True
            )


class ApplicationPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Become a Tester", style=discord.ButtonStyle.primary, custom_id="app_open_tester", emoji="⚔️")
    async def apply_tester(self, interaction: discord.Interaction, button: discord.ui.Button):
        warning_embed = discord.Embed(
            title="⚠️ Read Before Applying — Tester",
            description=(
                "By applying you agree to the following rules.\n"
                "**Violating any of these will result in an instant ban:**\n\n"
                "• Do not abuse your tester position\n"
                "• Do not provide false information in this application\n"
                "• Do not share or leak tier test results\n"
                "• Always remain professional and respectful during tests\n\n"
                "Click **I Understand — Apply Now** to continue."
            ),
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=warning_embed, view=TesterWarningView(), ephemeral=True)

    @discord.ui.button(label="Become Staff", style=discord.ButtonStyle.secondary, custom_id="app_open_staff", emoji="🛡️")
    async def apply_staff(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(StaffApplicationModal())

# ── Bot setup ─────────────────────────────────────────────────────────────────

class Client(discord.Client):
    async def setup_hook(self):
        self.add_view(TierTestView())
        self.add_view(CloseTicketView())
        self.add_view(ApplicationPanelView())
        self.add_view(ApplicationResultView())

    async def on_ready(self):
        init_db()
        for guild in self.guilds:
            tree.copy_global_to(guild=guild)
            await tree.sync(guild=guild)
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        print(f"Commands synced to {len(self.guilds)} guild(s).")

client = Client(intents=intents)
tree = app_commands.CommandTree(client)

# ── Commands ──────────────────────────────────────────────────────────────────

@tree.command(name="tier", description="Tier a user")
@tester_only()
@app_commands.choices(
    gamemode=[app_commands.Choice(name=g, value=g) for g in GAMEMODES],
    tier=[app_commands.Choice(name=t, value=t) for t in TIERS]
)
async def tier(
    interaction: discord.Interaction,
    user: discord.Member,
    tier: app_commands.Choice[str],
    gamemode: app_commands.Choice[str]
):
    gm = gamemode.value
    tr = tier.value

    role_name = f"{tr} {gm.upper()}"
    role = discord.utils.get(interaction.guild.roles, name=role_name)
    if role is None:
        role = await interaction.guild.create_role(name=role_name)

    old_roles = [
        r for r in user.roles
        if r.name.upper().endswith(f" {gm.upper()}") and r != role
    ]
    if old_roles:
        await user.remove_roles(*old_roles)

    await user.add_roles(role)

    ensure_user(str(user.id), user.name)
    ensure_user(str(interaction.user.id), interaction.user.name)
    insert_tier_record(str(user.id), gm, tr, str(interaction.user.id))

    channel = interaction.guild.get_channel(RESULT_CHANNELS.get(gm))
    embed = discord.Embed(
        title="Tier Result",
        description=f"{user.mention} got tiered",
        color=discord.Color.blurple()
    )
    embed.add_field(name="Tier", value=tr)
    embed.add_field(name="Gamemode", value=gm.upper())
    embed.add_field(name="Staff", value=interaction.user.mention)

    if channel:
        await channel.send(embed=embed)

    try:
        dm_embed = discord.Embed(
            title="You've Been Tiered!",
            description=f"Your tier result is in from **{interaction.guild.name}**.",
            color=discord.Color.blurple()
        )
        dm_embed.add_field(name="Gamemode", value=gm.upper(), inline=True)
        dm_embed.add_field(name="Tier", value=tr, inline=True)
        dm_embed.add_field(name="Tested by", value=interaction.user.display_name, inline=True)
        await user.send(embed=dm_embed)
    except discord.Forbidden:
        pass

    await interaction.response.send_message("done", ephemeral=True)


@tree.command(name="untier", description="Remove a user's tier in a gamemode")
@tester_only()
@app_commands.choices(
    gamemode=[app_commands.Choice(name=g, value=g) for g in GAMEMODES]
)
async def untier(
    interaction: discord.Interaction,
    user: discord.Member,
    gamemode: app_commands.Choice[str]
):
    gm = gamemode.value

    removed_roles = [
        r for r in user.roles
        if r.name.upper().endswith(f" {gm.upper()}")
    ]

    if not removed_roles:
        await interaction.response.send_message(
            f"{user.mention} has no tier in {gm.upper()}.", ephemeral=True
        )
        return

    await user.remove_roles(*removed_roles)

    channel = interaction.guild.get_channel(RESULT_CHANNELS.get(gm))
    embed = discord.Embed(
        title="Tier Removed",
        description=f"{user.mention} was untied",
        color=discord.Color.red()
    )
    removed_names = ", ".join(r.name for r in removed_roles)
    embed.add_field(name="Removed Tier", value=removed_names)
    embed.add_field(name="Gamemode", value=gm.upper())
    embed.add_field(name="Staff", value=interaction.user.mention)

    if channel:
        await channel.send(embed=embed)

    await interaction.response.send_message("done", ephemeral=True)


@tree.command(name="matchup", description="Compare tiers of two users in a gamemode")
@app_commands.choices(
    gamemode=[app_commands.Choice(name=g, value=g) for g in GAMEMODES]
)
async def matchup(
    interaction: discord.Interaction,
    user1: discord.Member,
    user2: discord.Member,
    gamemode: app_commands.Choice[str]
):
    gm = gamemode.value

    def get_tier(member):
        for role in member.roles:
            if role.name.upper().endswith(f" {gm.upper()}"):
                tier_name = role.name.upper().replace(f" {gm.upper()}", "").strip()
                if tier_name in TIERS:
                    return tier_name
        return "LT5"

    tier1 = get_tier(user1)
    tier2 = get_tier(user2)
    idx1 = TIERS.index(tier1)
    idx2 = TIERS.index(tier2)

    if idx1 > idx2:
        result = f"{user1.mention} is higher tier"
    elif idx2 > idx1:
        result = f"{user2.mention} is higher tier"
    else:
        result = "Both users are the same tier"

    embed = discord.Embed(title=f"Matchup — {gm.upper()}", color=discord.Color.gold())
    embed.add_field(name=user1.display_name, value=tier1, inline=True)
    embed.add_field(name="vs", value="\u200b", inline=True)
    embed.add_field(name=user2.display_name, value=tier2, inline=True)
    embed.add_field(name="Result", value=result, inline=False)

    await interaction.response.send_message(embed=embed)


@tree.command(name="history", description="Show the last 10 tier results for a user")
async def history(
    interaction: discord.Interaction,
    user: discord.Member
):
    rows = fetch_user_history(str(user.id), limit=10)
    embed = discord.Embed(
        title=f"Tier History — {user.display_name}",
        color=discord.Color.blurple()
    )
    if not rows:
        embed.description = "No tier history found for this user."
    else:
        lines = []
        for row in rows:
            ts = row["timestamp"][:10]
            lines.append(f"`{ts}` **{row['tier']}** in {row['gamemode'].upper()}")
        embed.description = "\n".join(lines)

    await interaction.response.send_message(embed=embed)


@tree.command(name="stats", description="Show a user's current tier across all gamemodes")
async def stats(
    interaction: discord.Interaction,
    user: discord.Member
):
    embed = discord.Embed(
        title=f"Stats — {user.display_name}",
        color=discord.Color.blurple()
    )

    lines = []
    for gm in GAMEMODES:
        current_tier = None
        for role in user.roles:
            if role.name.upper().endswith(f" {gm.upper()}"):
                tier_name = role.name.upper().replace(f" {gm.upper()}", "").strip()
                if tier_name in TIERS:
                    current_tier = tier_name
                    break
        if current_tier:
            lines.append(f"**{gm.upper()}** — {current_tier}")

    embed.description = "\n".join(lines) if lines else "No tiers found for this user."
    await interaction.response.send_message(embed=embed)


@tree.command(name="leaderboard", description="Show the top 10 players for a gamemode")
@app_commands.describe(gamemode="Gamemode to show (leave empty for overall)")
@app_commands.choices(gamemode=[app_commands.Choice(name=g, value=g) for g in GAMEMODES])
async def leaderboard(interaction: discord.Interaction, gamemode: app_commands.Choice[str] = None):
    gm = gamemode.value if gamemode else None
    with get_db() as conn:
        if gm:
            rows = conn.execute(
                """
                SELECT u.username, th.tier
                FROM tier_history th
                LEFT JOIN users u ON u.discord_id = th.user_id
                WHERE th.gamemode = ?
                  AND th.id = (
                      SELECT id FROM tier_history
                      WHERE user_id = th.user_id AND gamemode = th.gamemode
                      ORDER BY timestamp DESC LIMIT 1
                  )
                """,
                (gm,)
            ).fetchall()
            sorted_rows = sorted(rows, key=lambda r: TIER_RANK.get(r["tier"], -1), reverse=True)[:10]
            title = f"Leaderboard — {gm.upper()}"
            lines = [
                f"`#{i+1}` **{r['username']}** — {r['tier']}"
                for i, r in enumerate(sorted_rows)
                if TIER_RANK.get(r["tier"], -1) >= 0
            ]
        else:
            rows = conn.execute(
                """
                SELECT u.discord_id, u.username, th.gamemode, th.tier
                FROM tier_history th
                LEFT JOIN users u ON u.discord_id = th.user_id
                WHERE th.id = (
                    SELECT id FROM tier_history
                    WHERE user_id = th.user_id AND gamemode = th.gamemode
                    ORDER BY timestamp DESC LIMIT 1
                )
                """
            ).fetchall()
            player_map = {}
            for r in rows:
                score = TIER_RANK.get(r["tier"], -1)
                if score < 0:
                    continue
                existing = player_map.get(r["discord_id"])
                if not existing or score > existing["score"]:
                    player_map[r["discord_id"]] = {"username": r["username"], "tier": r["tier"], "gamemode": r["gamemode"], "score": score}
            top = sorted(player_map.values(), key=lambda p: p["score"], reverse=True)[:10]
            title = "Leaderboard — Overall"
            lines = [
                f"`#{i+1}` **{p['username']}** — {p['tier']} ({p['gamemode'].upper()})"
                for i, p in enumerate(top)
            ]

    embed = discord.Embed(title=title, color=discord.Color.gold())
    embed.description = "\n".join(lines) if lines else "No ranked players yet."
    await interaction.response.send_message(embed=embed)


@tree.command(name="rank", description="Show a player's rank position in a gamemode")
@app_commands.describe(user="The player to check", gamemode="The gamemode to check")
@app_commands.choices(gamemode=[app_commands.Choice(name=g, value=g) for g in GAMEMODES])
async def rank(interaction: discord.Interaction, user: discord.Member, gamemode: app_commands.Choice[str]):
    gm = gamemode.value
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT u.discord_id, th.tier
            FROM tier_history th
            LEFT JOIN users u ON u.discord_id = th.user_id
            WHERE th.gamemode = ?
              AND th.id = (
                  SELECT id FROM tier_history
                  WHERE user_id = th.user_id AND gamemode = th.gamemode
                  ORDER BY timestamp DESC LIMIT 1
              )
            """,
            (gm,)
        ).fetchall()

    sorted_rows = sorted(
        [r for r in rows if TIER_RANK.get(r["tier"], -1) >= 0],
        key=lambda r: TIER_RANK.get(r["tier"], -1),
        reverse=True
    )

    position = next((i + 1 for i, r in enumerate(sorted_rows) if r["discord_id"] == str(user.id)), None)
    user_tier = next((r["tier"] for r in sorted_rows if r["discord_id"] == str(user.id)), None)

    embed = discord.Embed(color=discord.Color.blurple())
    if position:
        embed.title = f"#{position} in {gm.upper()}"
        embed.description = f"{user.mention} is ranked **#{position}** out of **{len(sorted_rows)}** in {gm.upper()} with tier **{user_tier}**."
    else:
        embed.description = f"{user.mention} is not ranked in {gm.upper()} yet."

    await interaction.response.send_message(embed=embed)


@tree.command(name="unlink", description="Unlink your Minecraft account from Discord")
async def unlink(interaction: discord.Interaction):
    discord_id = str(interaction.user.id)
    with get_db() as conn:
        row = conn.execute(
            "SELECT mc_username FROM minecraft_links WHERE discord_id = ?", (discord_id,)
        ).fetchone()
        if not row:
            await interaction.response.send_message("You don't have a Minecraft account linked.", ephemeral=True)
            return
        conn.execute("DELETE FROM minecraft_links WHERE discord_id = ?", (discord_id,))
        conn.commit()

    await interaction.response.send_message(
        f"✅ Unlinked `{row['mc_username']}` from your Discord account.", ephemeral=True
    )


@tree.command(name="recent", description="Show the 10 most recent tier assignments")
async def recent(interaction: discord.Interaction):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT u.username, th.gamemode, th.tier, th.timestamp
            FROM tier_history th
            LEFT JOIN users u ON u.discord_id = th.user_id
            ORDER BY th.timestamp DESC LIMIT 10
            """
        ).fetchall()

    embed = discord.Embed(title="Recent Tier Assignments", color=discord.Color.blurple())
    if not rows:
        embed.description = "No tier assignments yet."
    else:
        lines = [
            f"`{r['timestamp'][:10]}` **{r['username']}** → {r['tier']} in {r['gamemode'].upper()}"
            for r in rows
        ]
        embed.description = "\n".join(lines)
    await interaction.response.send_message(embed=embed)


@tree.command(name="compare", description="Compare two players' tiers across all gamemodes")
@app_commands.describe(user1="First player", user2="Second player")
async def compare(interaction: discord.Interaction, user1: discord.Member, user2: discord.Member):
    def get_tiers(discord_id: str, conn):
        rows = conn.execute(
            """
            SELECT th.gamemode, th.tier
            FROM tier_history th
            WHERE th.user_id = ?
              AND th.id = (
                  SELECT id FROM tier_history
                  WHERE user_id = th.user_id AND gamemode = th.gamemode
                  ORDER BY timestamp DESC LIMIT 1
              )
            """,
            (discord_id,)
        ).fetchall()
        return {r["gamemode"]: r["tier"] for r in rows}

    with get_db() as conn:
        tiers1 = get_tiers(str(user1.id), conn)
        tiers2 = get_tiers(str(user2.id), conn)

    all_gms = [gm for gm in GAMEMODES if gm in tiers1 or gm in tiers2]
    if not all_gms:
        await interaction.response.send_message("Neither player has any tiers yet.", ephemeral=True)
        return

    embed = discord.Embed(title=f"{user1.display_name} vs {user2.display_name}", color=discord.Color.orange())
    lines = []
    for gm in all_gms:
        t1 = tiers1.get(gm, "—")
        t2 = tiers2.get(gm, "—")
        s1 = TIER_RANK.get(t1, -1)
        s2 = TIER_RANK.get(t2, -1)
        if s1 > s2:
            indicator = "◀"
        elif s2 > s1:
            indicator = "▶"
        else:
            indicator = "="
        lines.append(f"**{gm.upper()}** — {t1} {indicator} {t2}")

    embed.description = "\n".join(lines)
    embed.set_footer(text=f"◀ = {user1.display_name} wins  ▶ = {user2.display_name} wins  = = tied")
    await interaction.response.send_message(embed=embed)


@tree.command(name="tierlist", description="Show all ranked players grouped by tier for a gamemode")
@app_commands.describe(gamemode="The gamemode to show")
@app_commands.choices(gamemode=[app_commands.Choice(name=g, value=g) for g in GAMEMODES])
async def tierlist(interaction: discord.Interaction, gamemode: app_commands.Choice[str]):
    gm = gamemode.value
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT u.username, th.tier
            FROM tier_history th
            LEFT JOIN users u ON u.discord_id = th.user_id
            WHERE th.gamemode = ?
              AND th.id = (
                  SELECT id FROM tier_history
                  WHERE user_id = th.user_id AND gamemode = th.gamemode
                  ORDER BY timestamp DESC LIMIT 1
              )
            """,
            (gm,)
        ).fetchall()

    grouped: dict[str, list[str]] = {}
    for r in rows:
        t = r["tier"]
        if t in TIERS:
            grouped.setdefault(t, []).append(r["username"])

    embed = discord.Embed(title=f"Tier List — {gm.upper()}", color=discord.Color.green())
    if not grouped:
        embed.description = "No ranked players yet."
    else:
        for tier in reversed(TIERS):
            if tier in grouped:
                embed.add_field(
                    name=tier,
                    value=", ".join(f"**{u}**" for u in grouped[tier]),
                    inline=False
                )
    await interaction.response.send_message(embed=embed)


@tree.command(name="mylink", description="Check your own linked Minecraft account")
async def mylink(interaction: discord.Interaction):
    with get_db() as conn:
        row = conn.execute(
            "SELECT mc_username, linked_at FROM minecraft_links WHERE discord_id = ?",
            (str(interaction.user.id),)
        ).fetchone()

    if not row:
        await interaction.response.send_message(
            "You haven't linked a Minecraft account yet. Use `/link <username>` to link one.",
            ephemeral=True
        )
        return

    embed = discord.Embed(title="Your Minecraft Link", color=discord.Color.green())
    embed.add_field(name="IGN", value=f"`{row['mc_username']}`", inline=True)
    embed.add_field(name="Linked", value=row["linked_at"][:10], inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="testers", description="Show which testers have done the most tier tests")
async def testers(interaction: discord.Interaction):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT u.username, COUNT(*) as tests
            FROM tier_history th
            LEFT JOIN users u ON u.discord_id = th.tester_id
            GROUP BY th.tester_id
            ORDER BY tests DESC LIMIT 10
            """
        ).fetchall()

    embed = discord.Embed(title="Top Testers", color=discord.Color.gold())
    if not rows:
        embed.description = "No tests recorded yet."
    else:
        lines = [
            f"`#{i+1}` **{r['username'] or 'Unknown'}** — {r['tests']} test{'s' if r['tests'] != 1 else ''}"
            for i, r in enumerate(rows)
        ]
        embed.description = "\n".join(lines)
    await interaction.response.send_message(embed=embed)


@tree.command(name="removecooldown", description="Remove a player's cooldown so they can retest immediately")
@tester_only()
@app_commands.describe(user="The player to clear", gamemode="Gamemode to clear (leave empty for all)")
@app_commands.choices(gamemode=[app_commands.Choice(name=g, value=g) for g in GAMEMODES])
async def removecooldown(interaction: discord.Interaction, user: discord.Member, gamemode: app_commands.Choice[str] = None):
    discord_id = str(user.id)
    gm = gamemode.value if gamemode else None
    with get_db() as conn:
        if gm:
            conn.execute(
                "UPDATE tier_history SET timestamp = ? WHERE user_id = ? AND gamemode = ? AND id = ("
                "SELECT id FROM tier_history WHERE user_id = ? AND gamemode = ? ORDER BY timestamp DESC LIMIT 1)",
                (
                    (datetime.utcnow() - timedelta(days=31)).isoformat(),
                    discord_id, gm, discord_id, gm
                )
            )
            conn.commit()
            await interaction.response.send_message(
                f"✅ Removed **{gm.upper()}** cooldown for {user.mention}.", ephemeral=True
            )
        else:
            for g in GAMEMODES:
                conn.execute(
                    "UPDATE tier_history SET timestamp = ? WHERE user_id = ? AND gamemode = ? AND id = ("
                    "SELECT id FROM tier_history WHERE user_id = ? AND gamemode = ? ORDER BY timestamp DESC LIMIT 1)",
                    (
                        (datetime.utcnow() - timedelta(days=31)).isoformat(),
                        discord_id, g, discord_id, g
                    )
                )
            conn.commit()
            await interaction.response.send_message(
                f"✅ Removed **all** cooldowns for {user.mention}.", ephemeral=True
            )


@tree.command(name="activity", description="Show tier testing activity stats")
async def activity(interaction: discord.Interaction):
    now = datetime.utcnow()
    month_start = (now - timedelta(days=30)).isoformat()
    week_start = (now - timedelta(days=7)).isoformat()

    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) as c FROM tier_history").fetchone()["c"]
        this_month = conn.execute(
            "SELECT COUNT(*) as c FROM tier_history WHERE timestamp >= ?", (month_start,)
        ).fetchone()["c"]
        this_week = conn.execute(
            "SELECT COUNT(*) as c FROM tier_history WHERE timestamp >= ?", (week_start,)
        ).fetchone()["c"]
        total_players = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
        active_tickets = conn.execute(
            "SELECT COUNT(*) as c FROM tier_history WHERE timestamp >= ?",
            ((now - timedelta(hours=48)).isoformat(),)
        ).fetchone()["c"]
        top_gm = conn.execute(
            "SELECT gamemode, COUNT(*) as c FROM tier_history WHERE timestamp >= ? GROUP BY gamemode ORDER BY c DESC LIMIT 1",
            (month_start,)
        ).fetchone()

    embed = discord.Embed(title="Tier Testing Activity", color=discord.Color.blurple())
    embed.add_field(name="Total Tests", value=str(total), inline=True)
    embed.add_field(name="Last 7 Days", value=str(this_week), inline=True)
    embed.add_field(name="Last 30 Days", value=str(this_month), inline=True)
    embed.add_field(name="Total Players", value=str(total_players), inline=True)
    embed.add_field(name="Tests (48h)", value=str(active_tickets), inline=True)
    if top_gm:
        embed.add_field(name="Hottest Gamemode (30d)", value=f"{top_gm['gamemode'].upper()} ({top_gm['c']} tests)", inline=True)
    await interaction.response.send_message(embed=embed)


@tree.command(name="tiercount", description="Show how many players are ranked per gamemode")
async def tiercount(interaction: discord.Interaction):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT th.gamemode, COUNT(DISTINCT th.user_id) as players
            FROM tier_history th
            WHERE th.id = (
                SELECT id FROM tier_history
                WHERE user_id = th.user_id AND gamemode = th.gamemode
                ORDER BY timestamp DESC LIMIT 1
            )
            GROUP BY th.gamemode
            ORDER BY players DESC
            """
        ).fetchall()
        total_unique = conn.execute(
            "SELECT COUNT(DISTINCT user_id) as c FROM tier_history"
        ).fetchone()["c"]

    embed = discord.Embed(title="Ranked Players by Gamemode", color=discord.Color.gold())
    lines = [f"**{r['gamemode'].upper()}** — {r['players']} player{'s' if r['players'] != 1 else ''}" for r in rows]
    embed.description = "\n".join(lines) if lines else "No ranked players yet."
    embed.set_footer(text=f"Total unique players ranked: {total_unique}")
    await interaction.response.send_message(embed=embed)


@tree.command(name="announce", description="Send an announcement embed to a channel")
@tester_only()
@app_commands.describe(channel="Channel to send to", title="Announcement title", message="Announcement content", color="Color (red/green/blue/gold/default)")
@app_commands.choices(color=[
    app_commands.Choice(name="Blue", value="blue"),
    app_commands.Choice(name="Green", value="green"),
    app_commands.Choice(name="Red", value="red"),
    app_commands.Choice(name="Gold", value="gold"),
])
async def announce(interaction: discord.Interaction, channel: discord.TextChannel, title: str, message: str, color: app_commands.Choice[str] = None):
    colors = {"blue": discord.Color.blue(), "green": discord.Color.green(), "red": discord.Color.red(), "gold": discord.Color.gold()}
    c = colors.get(color.value if color else "blue", discord.Color.blue())
    embed = discord.Embed(title=title, description=message, color=c)
    embed.set_footer(text=f"Posted by {interaction.user.display_name}")
    await channel.send(embed=embed)
    await interaction.response.send_message(f"✅ Announcement sent to {channel.mention}.", ephemeral=True)


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


@tree.command(name="pendingtickets", description="List all currently open tier test tickets")
@tester_only()
async def pendingtickets(interaction: discord.Interaction):
    category = interaction.guild.get_channel(TICKET_CATEGORY_ID)
    if not category or not isinstance(category, discord.CategoryChannel):
        await interaction.response.send_message("Ticket category not found.", ephemeral=True)
        return

    tickets = [ch for ch in category.channels if ch.name.startswith("ticket-")]

    embed = discord.Embed(
        title=f"Open Tickets ({len(tickets)})",
        color=discord.Color.blurple()
    )
    if not tickets:
        embed.description = "No open tickets right now. ✅"
    else:
        lines = []
        for ch in sorted(tickets, key=lambda c: c.created_at):
            ts = int(ch.created_at.timestamp())
            lines.append(f"{ch.mention} — opened <t:{ts}:R>")
        embed.description = "\n".join(lines)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="note", description="Add a private staff note to a player's profile")
@tester_only()
@app_commands.describe(user="The player to note", text="The note content")
async def note(interaction: discord.Interaction, user: discord.Member, text: str):
    ensure_user(str(user.id), user.name)
    ensure_user(str(interaction.user.id), interaction.user.name)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO player_notes (user_id, author_id, note, created_at) VALUES (?, ?, ?, ?)",
            (str(user.id), str(interaction.user.id), text, datetime.utcnow().isoformat())
        )
        conn.commit()
    await interaction.response.send_message(
        f"✅ Note added to **{user.display_name}**'s profile.", ephemeral=True
    )


@tree.command(name="notes", description="View all staff notes on a player")
@tester_only()
@app_commands.describe(user="The player to view notes for")
async def notes(interaction: discord.Interaction, user: discord.Member):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT pn.note, u.username as author, pn.created_at
            FROM player_notes pn
            LEFT JOIN users u ON u.discord_id = pn.author_id
            WHERE pn.user_id = ?
            ORDER BY pn.created_at DESC
            """,
            (str(user.id),)
        ).fetchall()

    embed = discord.Embed(title=f"Staff Notes — {user.display_name}", color=discord.Color.blurple())
    if not rows:
        embed.description = "No notes for this player."
    else:
        lines = [
            f"`{r['created_at'][:10]}` **{r['author'] or 'Unknown'}**: {r['note']}"
            for r in rows
        ]
        embed.description = "\n".join(lines)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="resetuser", description="Wipe all tier data and roles for a player")
@tester_only()
@app_commands.describe(user="The player to reset")
async def resetuser(interaction: discord.Interaction, user: discord.Member):
    tier_roles = [
        r for r in user.roles
        if any(r.name.upper().endswith(f" {gm.upper()}") for gm in GAMEMODES)
    ]
    if tier_roles:
        await user.remove_roles(*tier_roles)

    with get_db() as conn:
        conn.execute("DELETE FROM tier_history WHERE user_id = ?", (str(user.id),))
        conn.commit()

    role_count = len(tier_roles)
    await interaction.response.send_message(
        f"✅ Reset complete for {user.mention} — removed {role_count} tier role(s) and cleared all tier history.",
        ephemeral=True
    )


@tree.command(name="healthcheck", description="Show bot latency, database status, and system info")
async def healthcheck(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    ws_latency = round(client.latency * 1000)

    import time
    start = time.monotonic()
    try:
        with get_db() as conn:
            conn.execute("SELECT 1").fetchone()
        db_ms = round((time.monotonic() - start) * 1000)
        db_status = f"✅ OK ({db_ms}ms)"
    except Exception as e:
        db_status = f"❌ Error: {e}"

    guilds = len(client.guilds)
    members = sum(g.member_count or 0 for g in client.guilds)

    if ws_latency < 100:
        latency_icon = "🟢"
    elif ws_latency < 250:
        latency_icon = "🟡"
    else:
        latency_icon = "🔴"

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
@tester_only()
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
@tester_only()
@app_commands.describe(channel="Channel to send the application panel to")
async def sendapplication(interaction: discord.Interaction, channel: discord.TextChannel):
    embed = discord.Embed(
        title="Join the Team",
        description=(
            "Want to become part of **Dropper Tiers**? Apply below.\n\n"
            "⚔️ **Tester** — Test players' PvP skills and assign tiers across gamemodes.\n"
            "🛡️ **Staff** — Help moderate and manage the community.\n\n"
            "*All applications are reviewed by the admin team. "
            "False information or rule violations will result in an instant ban.*"
        ),
        color=discord.Color.blurple()
    )
    await channel.send(embed=embed, view=ApplicationPanelView())
    await interaction.response.send_message(f"✅ Application panel sent to {channel.mention}.", ephemeral=True)


@tree.command(name="sendpanel", description="Send the tier test panel to the panel channel")
async def sendpanel(interaction: discord.Interaction):
    channel = interaction.guild.get_channel(PANEL_CHANNEL_ID)
    if not channel:
        await interaction.response.send_message("Panel channel not found.", ephemeral=True)
        return

    embed = discord.Embed(
        title="Tier Test",
        description="Want to get tiered? Click the button below to open a ticket.\n\nYou'll be asked for your **IGN**, **current tier**, and **preferred server**.",
        color=discord.Color.blurple()
    )

    await channel.send(embed=embed, view=TierTestView())
    await interaction.response.send_message("Panel sent!", ephemeral=True)

@tree.command(name="link", description="Link your Minecraft account to your Discord")
@app_commands.describe(minecraft_username="Your Minecraft IGN (case-sensitive)")
async def link(interaction: discord.Interaction, minecraft_username: str):
    discord_id = str(interaction.user.id)
    with get_db() as conn:
        existing = conn.execute(
            "SELECT mc_username FROM minecraft_links WHERE discord_id = ?", (discord_id,)
        ).fetchone()
        taken = conn.execute(
            "SELECT discord_id FROM minecraft_links WHERE mc_username = ?", (minecraft_username,)
        ).fetchone()

        if taken and taken["discord_id"] != discord_id:
            await interaction.response.send_message(
                f"❌ `{minecraft_username}` is already linked to another Discord account.", ephemeral=True
            )
            return

        conn.execute(
            "INSERT INTO minecraft_links (discord_id, mc_username, linked_at) VALUES (?, ?, ?)"
            " ON CONFLICT(discord_id) DO UPDATE SET mc_username=excluded.mc_username, linked_at=excluded.linked_at",
            (discord_id, minecraft_username, datetime.utcnow().isoformat())
        )
        conn.commit()

    verb = "updated" if existing else "linked"
    await interaction.response.send_message(
        f"✅ Successfully {verb} your Minecraft account: `{minecraft_username}`", ephemeral=True
    )


@tree.command(name="checklink", description="Check which Minecraft account is linked to a Discord user")
@app_commands.describe(user="The Discord user to check (leave empty to check yourself)")
async def checklink(interaction: discord.Interaction, user: discord.Member = None):
    target = user or interaction.user
    with get_db() as conn:
        row = conn.execute(
            "SELECT mc_username, linked_at FROM minecraft_links WHERE discord_id = ?",
            (str(target.id),)
        ).fetchone()

    if not row:
        msg = "You haven't linked a Minecraft account yet. Use `/link` to link one." if not user else f"{target.mention} hasn't linked a Minecraft account."
        await interaction.response.send_message(msg, ephemeral=True)
        return

    embed = discord.Embed(title="Minecraft Link", color=discord.Color.green())
    embed.add_field(name="Discord", value=target.mention, inline=True)
    embed.add_field(name="Minecraft IGN", value=f"`{row['mc_username']}`", inline=True)
    embed.add_field(name="Linked At", value=row["linked_at"][:10], inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Web health check + public API ─────────────────────────────────────────────

TIER_RANK = {t: i for i, t in enumerate(TIERS)}

def _get_current_tiers(discord_id: str, conn):
    return conn.execute(
        """
        SELECT th.gamemode, th.tier, th.timestamp
        FROM tier_history th
        WHERE th.user_id = ?
          AND th.id = (
              SELECT id FROM tier_history
              WHERE user_id = th.user_id AND gamemode = th.gamemode
              ORDER BY timestamp DESC LIMIT 1
          )
        """,
        (discord_id,)
    ).fetchall()

async def health(request):
    return web.Response(text="ok")

async def api_health(request):
    return web.Response(text="ok")

async def api_leaderboard_overall(request):
    import json
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT u.discord_id, u.username, th.gamemode, th.tier
            FROM tier_history th
            LEFT JOIN users u ON u.discord_id = th.user_id
            WHERE th.id = (
                SELECT id FROM tier_history
                WHERE user_id = th.user_id AND gamemode = th.gamemode
                ORDER BY timestamp DESC LIMIT 1
            )
            """
        ).fetchall()

    player_map = {}
    for row in rows:
        score = TIER_RANK.get(row["tier"], -1)
        if score < 0:
            continue
        existing = player_map.get(row["discord_id"])
        if not existing or score > existing["score"]:
            player_map[row["discord_id"]] = {
                "discord_id": row["discord_id"],
                "username": row["username"] or "Unknown",
                "score": score,
                "tier": row["tier"],
                "gamemode": row["gamemode"],
            }

    sorted_players = sorted(player_map.values(), key=lambda p: p["score"], reverse=True)[:50]
    result = [{"rank": i + 1, **p} for i, p in enumerate(sorted_players)]
    return web.Response(text=json.dumps(result), content_type="application/json")

async def api_leaderboard_gamemode(request):
    import json
    gamemode = request.match_info["gamemode"].lower()
    if gamemode not in GAMEMODES:
        return web.Response(status=400, text=json.dumps({"error": "Invalid gamemode"}), content_type="application/json")

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT u.discord_id, u.username, th.tier, th.timestamp
            FROM tier_history th
            LEFT JOIN users u ON u.discord_id = th.user_id
            WHERE th.gamemode = ?
              AND th.id = (
                  SELECT id FROM tier_history
                  WHERE user_id = th.user_id AND gamemode = th.gamemode
                  ORDER BY timestamp DESC LIMIT 1
              )
            """,
            (gamemode,)
        ).fetchall()

    sorted_rows = sorted(rows, key=lambda r: TIER_RANK.get(r["tier"], -1), reverse=True)
    result = [
        {
            "rank": i + 1,
            "username": r["username"] or "Unknown",
            "discord_id": r["discord_id"],
            "tier": r["tier"],
            "gamemode": gamemode,
            "score": TIER_RANK.get(r["tier"], -1),
        }
        for i, r in enumerate(sorted_rows)
        if TIER_RANK.get(r["tier"], -1) >= 0
    ][:50]
    return web.Response(text=json.dumps(result), content_type="application/json")

async def api_players_search(request):
    import json

    q = (
        request.rel_url.query.get("q")
        or request.rel_url.query.get("ign")
        or ""
    ).strip()

    if not q:
        return web.Response(
            text=json.dumps([]),
            content_type="application/json"
        )

with get_db() as conn:
    print("DB:", DB_PATH)

    rows = conn.execute(
        "SELECT discord_id, mc_username FROM minecraft_links"
    ).fetchall()

    print("LINKS:", [dict(r) for r in rows])

    users = conn.execute(
        """
        SELECT discord_id, mc_username
        FROM minecraft_links
        WHERE mc_username LIKE ?
        LIMIT 20
        """,
        (f"%{q}%",)
    ).fetchall()

    result = []
    for u in users:
    best = conn.execute(
                """
                SELECT th.tier, th.gamemode
                FROM tier_history th
                WHERE th.user_id = ?
                  AND th.id = (
                      SELECT id FROM tier_history
                      WHERE user_id = th.user_id AND gamemode = th.gamemode
                      ORDER BY timestamp DESC LIMIT 1
                  )
                ORDER BY th.tier DESC LIMIT 1
                """,
                (u["discord_id"],)
            ).fetchone()
            result.append({
                "discord_id": u["discord_id"],
                "username": u["username"],
                "best_tier": best["tier"] if best else None,
                "best_gamemode": best["gamemode"] if best else None,
            })

    return web.Response(text=json.dumps(result), content_type="application/json")
    
async def api_player_profile(request):
    import json
    username = request.match_info["username"]

    with get_db() as conn:
        user = conn.execute(
            "SELECT discord_id, username FROM users WHERE username LIKE ? LIMIT 1",
            (username,)
        ).fetchone()

        if not user:
            return web.Response(status=404, text=json.dumps({"error": "Player not found"}), content_type="application/json")

        tiers = _get_current_tiers(user["discord_id"], conn)
        history = conn.execute(
            "SELECT gamemode, tier, timestamp, notes FROM tier_history WHERE user_id = ? ORDER BY timestamp DESC LIMIT 20",
            (user["discord_id"],)
        ).fetchall()

    result = {
        "discord_id": user["discord_id"],
        "username": user["username"],
        "tiers": [{"gamemode": r["gamemode"], "tier": r["tier"], "timestamp": r["timestamp"]} for r in tiers],
        "history": [{"gamemode": r["gamemode"], "tier": r["tier"], "timestamp": r["timestamp"], "notes": r["notes"]} for r in history],
    }
    return web.Response(text=json.dumps(result), content_type="application/json")


    return web.Response(
        text=json.dumps(result),
        content_type="application/json"
            )
async def api_minecraft_player(request):
    import json
    mc_username = request.match_info["ign"]
    with get_db() as conn:
        link = conn.execute(
            "SELECT discord_id FROM minecraft_links WHERE mc_username = ? COLLATE NOCASE LIMIT 1",
            (mc_username,)
        ).fetchone()
        if not link:
            return web.Response(status=404, text=json.dumps({"error": "No linked Discord account"}), content_type="application/json")

        discord_id = link["discord_id"]
        user = conn.execute("SELECT username FROM users WHERE discord_id = ?", (discord_id,)).fetchone()
        tiers = _get_current_tiers(discord_id, conn)

    result = {
        "mc_username": mc_username,
        "discord_id": discord_id,
        "discord_username": user["username"] if user else None,
        "tiers": [{"gamemode": r["gamemode"], "tier": r["tier"]} for r in tiers],
        "best_tier": max((r["tier"] for r in tiers), key=lambda t: TIER_RANK.get(t, -1), default=None),
    }
    return web.Response(text=json.dumps(result), content_type="application/json")


async def main():
    await restore_db_from_github()
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/api", api_health)
    app.router.add_get("/api/leaderboard/overall", api_leaderboard_overall)
    app.router.add_get("/api/leaderboard/{gamemode}", api_leaderboard_gamemode)
    app.router.add_get("/api/players/search", api_players_search)
    app.router.add_get("/api/players/{username}", api_player_profile)
    app.router.add_get("/api/minecraft/{ign}", api_minecraft_player)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT, reuse_port=True)
    await site.start()
    print(f"Health check running on port {PORT}")
    await client.start(TOKEN)

asyncio.run(main())












