"""Discord slash-command adapter for persistent moderation cases."""

import re
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands

from core.database import DatabaseError
from core.moderation import ModerationService


def register_moderation_commands(tree, database_provider, audit_recorder):
    def service():
        database = database_provider()
        if database is None:
            raise DatabaseError("The database is not available")
        return ModerationService(database)

    async def validate_target(interaction, target, *, require_bot_hierarchy=True):
        guild = interaction.guild
        actor = interaction.user
        bot_member = guild.me
        if target.id == guild.owner_id:
            await interaction.response.send_message("The server owner cannot be moderated.", ephemeral=True)
            return False
        if actor.id != guild.owner_id and target.top_role >= actor.top_role:
            await interaction.response.send_message("You cannot moderate a member with an equal or higher role.", ephemeral=True)
            return False
        if require_bot_hierarchy and (bot_member is None or target.top_role >= bot_member.top_role):
            await interaction.response.send_message("My highest role must be above the target's highest role.", ephemeral=True)
            return False
        if target.id == actor.id:
            await interaction.response.send_message("You cannot moderate yourself.", ephemeral=True)
            return False
        return True

    async def create_pending(interaction, target_id, action, reason, expires_at=None):
        return await service().create_case(
            guild_id=interaction.guild.id, target_id=target_id,
            moderator_id=interaction.user.id, action=action, reason=reason,
            expires_at=expires_at,
        )

    async def finish(case, status, failure=None):
        try:
            await service().complete_case(
                int(case["case_id"]), status=status, failure_reason=failure
            )
        except Exception as exc:
            print(f"Could not finalize moderation case {case.get('case_id')}: {type(exc).__name__}: {exc}")
            return False
        return True

    async def report_failure(interaction, case, exc):
        finalized = await finish(case, "failed", f"{type(exc).__name__}: {exc}")
        message = (
            f"Discord rejected the action. Failed attempt recorded as case #{case['case_id']}."
            if finalized else
            f"Discord rejected the action, but D1 could not finalize case #{case['case_id']}. It remains pending and needs review."
        )
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def report_success(interaction, case, target_label, extra=None):
        finalized = await finish(case, "applied")
        if not finalized:
            await interaction.followup.send(
                f"The Discord action succeeded, but D1 could not finalize case #{case['case_id']}. "
                "The case remains pending and requires staff review.",
                ephemeral=True,
            )
            return
        await audit_recorder(
            interaction.guild.id, interaction.user.id, f"moderation.{case['action']}",
            target_id=int(case["target_id"]),
            details={"case_id": int(case["case_id"]), "reason": case["reason"]},
        )
        description = f"**Case #{case['case_id']}** · **{case['action'].upper()}**\nTarget: {target_label}\nReason: {discord.utils.escape_markdown(case['reason'])}"
        if extra:
            description += f"\n{extra}"
        embed = discord.Embed(title="VANTA  /  MODERATION CASE", description=description, color=0x3978F6)
        embed.set_footer(text=f"Moderator: {interaction.user} · Case persisted in D1")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @tree.command(name="warn", description="Issue a persistent moderation warning")
    @app_commands.describe(member="Member to warn", reason="Reason for the warning")
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def warn(interaction: discord.Interaction, member: discord.Member, reason: app_commands.Range[str, 1, 512]):
        if not await validate_target(interaction, member, require_bot_hierarchy=False):
            return
        await interaction.response.defer(ephemeral=True)
        case = await create_pending(interaction, member.id, "warn", reason)
        try:
            try:
                await member.send(f"You received a moderation warning in **{interaction.guild.name}**.\nReason: {reason}")
            except (discord.Forbidden, discord.HTTPException):
                pass
            await report_success(interaction, case, member.mention, "The member could not necessarily receive a DM.")
        except Exception as exc:
            await report_failure(interaction, case, exc)

    @tree.command(name="timeout", description="Temporarily timeout a member and create a case")
    @app_commands.describe(member="Member to timeout", minutes="Duration in minutes (1–40320)", reason="Reason for the timeout")
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def timeout(interaction: discord.Interaction, member: discord.Member,
                      minutes: app_commands.Range[int, 1, 40320], reason: app_commands.Range[str, 1, 512]):
        if not await validate_target(interaction, member):
            return
        await interaction.response.defer(ephemeral=True)
        expires = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        case = await create_pending(interaction, member.id, "timeout", reason, expires.isoformat())
        try:
            await member.timeout(timedelta(minutes=minutes), reason=f"Vanta case #{case['case_id']}: {reason}")
            await report_success(interaction, case, member.mention, f"Duration: {minutes} minutes")
        except (discord.Forbidden, discord.HTTPException) as exc:
            await report_failure(interaction, case, exc)

    @tree.command(name="kick", description="Kick a member and create a persistent case")
    @app_commands.describe(member="Member to kick", reason="Reason for the kick")
    @app_commands.checks.has_permissions(kick_members=True)
    @app_commands.guild_only()
    async def kick(interaction: discord.Interaction, member: discord.Member, reason: app_commands.Range[str, 1, 512]):
        if not await validate_target(interaction, member):
            return
        await interaction.response.defer(ephemeral=True)
        case = await create_pending(interaction, member.id, "kick", reason)
        try:
            await member.kick(reason=f"Vanta case #{case['case_id']}: {reason}")
            await report_success(interaction, case, f"<@{member.id}>")
        except (discord.Forbidden, discord.HTTPException) as exc:
            await report_failure(interaction, case, exc)

    @tree.command(name="ban", description="Ban a member and create a persistent case")
    @app_commands.describe(member="Member to ban", reason="Reason for the ban", delete_message_days="Days of recent messages to delete (0–7)")
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.guild_only()
    async def ban(interaction: discord.Interaction, member: discord.Member,
                  reason: app_commands.Range[str, 1, 512], delete_message_days: app_commands.Range[int, 0, 7] = 0):
        if not await validate_target(interaction, member):
            return
        await interaction.response.defer(ephemeral=True)
        case = await create_pending(interaction, member.id, "ban", reason)
        try:
            await interaction.guild.ban(
                member, reason=f"Vanta case #{case['case_id']}: {reason}",
                delete_message_seconds=delete_message_days * 86400,
            )
            await report_success(interaction, case, f"<@{member.id}>")
        except (discord.Forbidden, discord.HTTPException) as exc:
            await report_failure(interaction, case, exc)

    @tree.command(name="unban", description="Unban a Discord user ID and create a persistent case")
    @app_commands.describe(user_id="Discord user ID to unban", reason="Reason for the unban")
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.guild_only()
    async def unban(interaction: discord.Interaction, user_id: str, reason: app_commands.Range[str, 1, 512]):
        if not re.fullmatch(r"[0-9]{15,22}", user_id):
            await interaction.response.send_message("Enter a valid Discord user ID.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        case = await create_pending(interaction, user_id, "unban", reason)
        try:
            await interaction.guild.unban(discord.Object(id=int(user_id)), reason=f"Vanta case #{case['case_id']}: {reason}")
            await report_success(interaction, case, f"<@{user_id}>")
        except (discord.Forbidden, discord.HTTPException, discord.NotFound) as exc:
            await report_failure(interaction, case, exc)

    @tree.command(name="cases", description="View recent moderation cases for a member")
    @app_commands.describe(member="Member whose moderation history you want to inspect")
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def cases(interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True)
        rows, total = await service().list_cases(interaction.guild.id, target_id=member.id, limit=10)
        embed = discord.Embed(
            title="VANTA  /  MODERATION HISTORY",
            description=f"Recent cases for {member.mention}",
            color=0x3978F6,
        )
        if not rows:
            embed.add_field(name="No cases", value="No moderation cases are recorded for this member.")
        else:
            lines = []
            for row in rows:
                status = row["status"].upper()
                reason_text = discord.utils.escape_markdown(row["reason"])[:180]
                lines.append(
                    f"**#{row['case_id']} · {row['action'].upper()} · {status}**\n"
                    f"{reason_text}\n"
                    f"<t:{int(datetime.fromisoformat(row['created_at'].replace('Z', '+00:00')).timestamp())}:R> · <@{row['moderator_id']}>"
                )
            embed.description = "\n\n".join(lines)
        embed.set_footer(text=f"{total} total case(s) · Latest 10 · Cloudflare D1")
        await interaction.followup.send(embed=embed, ephemeral=True)

