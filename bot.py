import os
import asyncio
import sqlite3
from datetime import datetime
import discord
from discord import app_commands
from aiohttp import web

TOKEN = os.environ["DISCORD_TOKEN"]
PORT = int(os.environ.get("PORT", 3000))
DB_PATH = "tiers.db"

PANEL_CHANNEL_ID = 1517249587931250760
TICKET_CATEGORY_ID = 1514689354281259170
TESTER_ROLE_ID = 1514260245034041485

intents = discord.Intents.default()

# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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
        channel = await guild.create_text_channel(
            name=f"ticket-{safe_name}",
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
        await interaction.channel.delete()

# ── Bot setup ─────────────────────────────────────────────────────────────────

class Client(discord.Client):
    async def setup_hook(self):
        self.add_view(TierTestView())
        self.add_view(CloseTicketView())

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

# ── Web health check ──────────────────────────────────────────────────────────

async def health(request):
    return web.Response(text="ok")

async def main():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Health check running on port {PORT}")
    await client.start(TOKEN)

asyncio.run(main())
