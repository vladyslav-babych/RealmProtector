from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from src.realm_protector.bot.common import (
    allowed_user_mentions,
    parse_csv_values,
    send_followup_lines,
)
from src.realm_protector.infrastructure import (
    credential_store,
    guild_settings,
    local_repository,
)
from src.realm_protector.services import google_sync, guild_lifecycle, request_limits
from src.realm_protector.services.authorization import is_admin
from src.realm_protector.services.keyed_locks import KeyedLockPool
from src.realm_protector.services.role_security import (
    member_has_safe_privileged_role,
)

if TYPE_CHECKING:
    from src.realm_protector.bot.client import RealmProtectorBot


_balance_lookup_cooldown = request_limits.Cooldown(10)
_leaderboard_message_locks: KeyedLockPool[int] = KeyedLockPool()
MAX_SILVER_TRANSACTION = 10_000_000_000_000
MAX_REASON_LENGTH = 500
MAX_CONTENT_NAME_LENGTH = 200
LEADERBOARD_PAGE_SIZE = 10
_LEADERBOARD_FOOTER_PATTERN = re.compile(r"Page ([1-9][0-9]*)/([1-9][0-9]*)")
LOGGER = logging.getLogger(__name__)


def _active_ledger_id(discord_guild_id: int) -> int:
    if not guild_settings.get_target_guild(discord_guild_id):
        raise RuntimeError("This Discord server is not configured for an Albion guild.")
    ledger_id = local_repository.get_active_ledger_id(
        discord_guild_id,
        create_if_missing=False,
    )
    if ledger_id is None:
        raise RuntimeError("No active local ledger is configured for this server.")
    return ledger_id


def _has_named_role(member: discord.Member, role_names: list[str]) -> bool:
    return member_has_safe_privileged_role(member, role_names=role_names)


async def _has_economy_access(member: discord.Member, guild_id: int) -> bool:
    if await is_admin(member):
        return True
    configured_role_ids = guild_settings.get_economy_manager_role_ids(guild_id)
    if configured_role_ids:
        return member_has_safe_privileged_role(
            member,
            role_ids=configured_role_ids,
        )
    return _has_named_role(
        member,
        guild_settings.get_economy_manager_roles(guild_id),
    )


async def _ensure_local_ledger_ready(interaction: discord.Interaction) -> bool:
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "This command can only be used inside a server.",
            ephemeral=True,
        )
        return False
    if not await asyncio.to_thread(guild_settings.get_target_guild, guild.id):
        await interaction.response.send_message(
            "This server is not configured yet. Run **/bot-setup** first.",
            ephemeral=True,
        )
        return False
    if await asyncio.to_thread(google_sync.is_cutover_ready, guild.id):
        return True
    await interaction.response.send_message(
        "The one-time Google Sheet migration is still pending. Local balance workflows will be available after it completes.",
        ephemeral=True,
    )
    return False


def _local_ledger_unavailable_message(guild_id: int) -> Optional[str]:
    if not guild_settings.get_target_guild(guild_id):
        return "This server is not configured yet. Run **/bot-setup** first."
    if not google_sync.is_cutover_ready(guild_id):
        return (
            "The one-time Google Sheet migration is still pending. Local balance "
            "workflows will be available after it completes."
        )
    return None


def _resolve_member_local_name(
    ledger_id: int,
    member: discord.Member,
) -> str:
    player = local_repository.get_player(ledger_id, member.id)
    return player.nickname if player is not None else member.display_name


def _format_siphon(snapshot, *, google_linked: bool) -> str:
    if not google_linked:
        return "Unavailable (Google Sheet not linked)"
    if snapshot.siphon is None or snapshot.siphon_revision != snapshot.revision:
        return "Pending Google calculation"
    value = f"{snapshot.siphon:,} :oil:"
    if not snapshot.siphon_synced_at:
        return value
    try:
        synchronized_at = datetime.fromisoformat(snapshot.siphon_synced_at)
        if synchronized_at.tzinfo is None:
            synchronized_at = synchronized_at.replace(tzinfo=timezone.utc)
        age_seconds = (
            datetime.now(timezone.utc) - synchronized_at.astimezone(timezone.utc)
        ).total_seconds()
    except (TypeError, ValueError):
        return value + " (sync time unknown)"
    if age_seconds > google_sync.SIPHON_STALE_AFTER_SECONDS:
        return value + " (stale)"
    return value


def _build_balance_embed(
    target: discord.Member | discord.User,
    snapshot: local_repository.BalanceSnapshot,
    *,
    google_linked: bool,
    leaderboard_position: Optional[int] = None,
) -> discord.Embed:
    embed = discord.Embed(
        color=discord.Color.gold(),
        description=f"### {target.mention} balance:",
    )
    display_avatar = getattr(target, "display_avatar", None)
    avatar_url = getattr(display_avatar, "url", None)
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)
    embed.add_field(name="Balance", value=f"{snapshot.silver:,} :coin:", inline=True)
    embed.add_field(
        name="Siphon",
        value=_format_siphon(snapshot, google_linked=google_linked),
        inline=True,
    )
    embed.add_field(
        name="All-time earnings",
        value=f"{snapshot.all_time_earnings:,} :coin:",
        inline=False,
    )
    embed.add_field(name="Raw balance", value=str(snapshot.silver), inline=False)
    if leaderboard_position is not None:
        embed.set_footer(text=f"Leaderboard position: #{leaderboard_position}")
    return embed


async def _load_balance_embed(
    discord_guild_id: int,
    target: discord.Member | discord.User,
) -> Optional[discord.Embed]:
    """Load and render the shared balance view for prefix and slash commands."""

    async with guild_lifecycle.lock_for(discord_guild_id):
        ledger_id = await asyncio.to_thread(_active_ledger_id, discord_guild_id)
        snapshot = await asyncio.to_thread(
            local_repository.get_balance_snapshot,
            ledger_id,
            target.id,
        )
        if snapshot is None:
            return None
        leaderboard_position = await asyncio.to_thread(
            local_repository.get_silver_leaderboard_position,
            ledger_id,
            target.id,
        )

    google_linked = bool(
        await asyncio.to_thread(
            credential_store.get_credentials_info,
            discord_guild_id,
        )
    )
    return _build_balance_embed(
        target,
        snapshot,
        google_linked=google_linked,
        leaderboard_position=leaderboard_position,
    )


def _leaderboard_total_pages(total_players: int) -> int:
    return max(1, (total_players + LEADERBOARD_PAGE_SIZE - 1) // LEADERBOARD_PAGE_SIZE)


def _build_leaderboard_embed(
    page: local_repository.SilverLeaderboardPage,
    *,
    page_number: int,
) -> discord.Embed:
    embed = discord.Embed(color=discord.Color.gold(), title="Silver leaderboard")
    if page.players:
        embed.description = "\n".join(
            f"{rank}. <@{player.discord_user_id}> - :coin: {player.silver:,}"
            for rank, player in enumerate(page.players, start=page.offset + 1)
        )
    else:
        embed.description = "No registered players."
    embed.set_footer(text=f"Page {page_number}/{_leaderboard_total_pages(page.total_players)}")
    return embed


def _leaderboard_page_from_message(message: discord.Message) -> Optional[int]:
    if not message.embeds:
        return None
    footer_text = message.embeds[0].footer.text
    if not footer_text:
        return None
    match = _LEADERBOARD_FOOTER_PATTERN.fullmatch(footer_text)
    return int(match.group(1)) if match else None


async def _send_ephemeral_notice(
    interaction: discord.Interaction,
    message: str,
) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


async def _navigate_leaderboard(
    interaction: discord.Interaction,
    direction: int,
) -> None:
    guild = interaction.guild
    message = interaction.message
    if guild is None or message is None:
        await _send_ephemeral_notice(
            interaction,
            "This leaderboard button can only be used inside a server.",
        )
        return

    current_page = _leaderboard_page_from_message(message)
    if current_page is None:
        await _send_ephemeral_notice(
            interaction,
            "This leaderboard panel is no longer valid. Run **!lb** again.",
        )
        return

    await interaction.response.defer()
    async with _leaderboard_message_locks.hold(message.id):
        try:
            # Re-fetch under the per-message lock so simultaneous clicks build
            # on the page written by the interaction immediately before them.
            latest_message = await message.channel.fetch_message(message.id)
            current_page = _leaderboard_page_from_message(latest_message) or current_page
            async with guild_lifecycle.lock_for(guild.id):
                ledger_id = await asyncio.to_thread(_active_ledger_id, guild.id)
                count_page = await asyncio.to_thread(
                    local_repository.get_silver_leaderboard,
                    ledger_id,
                    limit=LEADERBOARD_PAGE_SIZE,
                    offset=0,
                )
                total_pages = _leaderboard_total_pages(count_page.total_players)
                target_page = min(total_pages, max(1, current_page + direction))
                page = await asyncio.to_thread(
                    local_repository.get_silver_leaderboard,
                    ledger_id,
                    limit=LEADERBOARD_PAGE_SIZE,
                    offset=(target_page - 1) * LEADERBOARD_PAGE_SIZE,
                )
        except Exception:
            LOGGER.exception("Leaderboard navigation failed in guild %s", guild.id)
            await interaction.followup.send(
                "Failed to refresh the leaderboard. Run **!lb** again.",
                ephemeral=True,
            )
            return

        await latest_message.edit(
            embed=_build_leaderboard_embed(page, page_number=target_page),
            view=LeaderboardView(target_page, total_pages),
            allowed_mentions=discord.AllowedMentions.none(),
        )


class _LeaderboardNavigationButton(discord.ui.Button):
    def __init__(self, *, direction: int, disabled: bool) -> None:
        self.direction = direction
        super().__init__(
            label="Previous" if direction < 0 else "Next",
            style=discord.ButtonStyle.secondary,
            custom_id=(
                "realm-protector:leaderboard:previous"
                if direction < 0
                else "realm-protector:leaderboard:next"
            ),
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await _navigate_leaderboard(interaction, self.direction)


class LeaderboardView(discord.ui.View):
    def __init__(self, current_page: int = 1, total_pages: int = 1) -> None:
        super().__init__(timeout=None)
        self.add_item(
            _LeaderboardNavigationButton(
                direction=-1,
                disabled=current_page <= 1,
            )
        )
        self.add_item(
            _LeaderboardNavigationButton(
                direction=1,
                disabled=current_page >= total_pages,
            )
        )


def register_economy_persistent_views(bot: "RealmProtectorBot") -> None:
    bot.add_view(LeaderboardView())


def _build_balance_update_embed(
    actor: discord.Member,
    target: discord.Member,
    action_text: str,
    amount_text: str,
    reason: str,
    old_balance: int,
    new_balance: int,
    *,
    history_failed: bool = False,
) -> discord.Embed:
    direction = "to" if action_text == "added" else "from"
    embed = discord.Embed(
        color=discord.Color.blurple(),
        description=(
            f"### {actor.mention} {action_text} {amount_text} balance {direction} {target.mention}"
        ),
    )
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Old balance", value=f"{old_balance:,} :coin:", inline=False)
    embed.add_field(name="New balance", value=f"{new_balance:,} :coin:", inline=False)
    if history_failed:
        embed.set_footer(text="Balance History entry could not be written.")
    return embed


async def _handle_balance_change(
    interaction: discord.Interaction,
    member: discord.Member,
    raw_amount: str,
    reason: str,
    *,
    option_name: str,
    remove: bool,
) -> None:
    guild = interaction.guild
    actor = interaction.user
    if guild is None or not isinstance(actor, discord.Member):
        await interaction.response.send_message(
            "This command can only be used inside a server.",
            ephemeral=True,
        )
        return

    if not await _has_economy_access(actor, guild.id):
        await interaction.response.send_message(
            "You don't have permission to use this command.",
            ephemeral=True,
        )
        return
    if not await _ensure_local_ledger_ready(interaction):
        return

    normalized_amount = raw_amount.strip()
    if len(normalized_amount) > len(str(MAX_SILVER_TRANSACTION)):
        await interaction.response.send_message(
            f"`{option_name}` must not exceed {MAX_SILVER_TRANSACTION:,}.",
            ephemeral=True,
        )
        return
    try:
        requested_amount = int(normalized_amount)
    except ValueError:
        await interaction.response.send_message(
            f"`{option_name}` must be an integer.",
            ephemeral=True,
        )
        return

    if requested_amount <= 0:
        await interaction.response.send_message(
            f"`{option_name}` must be greater than 0.",
            ephemeral=True,
        )
        return
    if requested_amount > MAX_SILVER_TRANSACTION:
        await interaction.response.send_message(
            f"`{option_name}` must not exceed {MAX_SILVER_TRANSACTION:,}.",
            ephemeral=True,
        )
        return
    reason = reason.strip()
    if not reason or len(reason) > MAX_REASON_LENGTH:
        await interaction.response.send_message(
            f"`reason` must contain 1-{MAX_REASON_LENGTH} characters.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True)
    requested_delta = -requested_amount if remove else requested_amount
    async with guild_lifecycle.lock_for(guild.id):
        try:
            ledger_id = await asyncio.to_thread(_active_ledger_id, guild.id)
            actor_name = await asyncio.to_thread(
                _resolve_member_local_name,
                ledger_id,
                actor,
            )
            result = await asyncio.to_thread(
                local_repository.change_balance,
                ledger_id,
                member.id,
                requested_delta,
                actor_discord_user_id=actor.id,
                actor_name=actor_name,
                reason=reason,
                idempotency_key=(
                    f"discord:balance:{interaction.id}"
                    if getattr(interaction, "id", None)
                    else None
                ),
            )
        except Exception:
            LOGGER.exception("Local balance update failed in guild %s", guild.id)
            await interaction.followup.send("Failed to update the local balance. Try again.")
            return

    if result is None:
        await interaction.followup.send(f"{member.mention} is not registered.")
        return

    actual_delta = result.actual_delta
    action_text = "removed" if remove else "added"
    amount_text = f"{abs(actual_delta):,}"
    embed = _build_balance_update_embed(
        actor,
        member,
        action_text,
        amount_text,
        reason,
        result.previous_balance,
        result.updated_balance,
    )
    await interaction.followup.send(embed=embed)


def create_economy_commands(
    bot: "RealmProtectorBot",
) -> list[app_commands.Command]:
    @app_commands.command(
        name="lootsplit",
        description="Distribute lootsplit and save history rows",
    )
    @app_commands.guild_only()
    async def lootsplit(
        interaction: discord.Interaction,
        battle_ids: str,
        content_name: str,
        caller: discord.Member,
        participants: str,
        lootsplit_amount: str,
        officer: Optional[discord.Member] = None,
    ) -> None:
        guild = interaction.guild
        actor = interaction.user
        if guild is None or not isinstance(actor, discord.Member):
            await interaction.response.send_message(
                "This command can only be used inside a server.",
                ephemeral=True,
            )
            return
        if not await _has_economy_access(actor, guild.id):
            await interaction.response.send_message(
                "You don't have permission to use this command.",
                ephemeral=True,
            )
            return
        if not await _ensure_local_ledger_ready(interaction):
            return

        normalized_amount = lootsplit_amount.strip()
        if len(normalized_amount) > len(str(MAX_SILVER_TRANSACTION)):
            await interaction.response.send_message(
                f"`lootsplit_amount` must not exceed {MAX_SILVER_TRANSACTION:,}.",
                ephemeral=True,
            )
            return
        try:
            amount = int(normalized_amount)
        except ValueError:
            await interaction.response.send_message(
                "`lootsplit_amount` must be an integer.",
                ephemeral=True,
            )
            return
        if amount <= 0:
            await interaction.response.send_message(
                "`lootsplit_amount` must be greater than 0.",
                ephemeral=True,
            )
            return
        if amount > MAX_SILVER_TRANSACTION:
            await interaction.response.send_message(
                f"`lootsplit_amount` must not exceed {MAX_SILVER_TRANSACTION:,}.",
                ephemeral=True,
            )
            return
        content_name = content_name.strip()
        if not content_name or len(content_name) > MAX_CONTENT_NAME_LENGTH:
            await interaction.response.send_message(
                f"`content_name` must contain 1-{MAX_CONTENT_NAME_LENGTH} characters.",
                ephemeral=True,
            )
            return

        battle_id_list = parse_csv_values(battle_ids)
        participant_list = parse_csv_values(participants, deduplicate=True)
        if not battle_id_list:
            await interaction.response.send_message(
                "Please provide at least one battle ID.",
                ephemeral=True,
            )
            return
        if not participant_list:
            await interaction.response.send_message(
                "Please provide at least one participant.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        effective_officer = officer or actor
        async with guild_lifecycle.lock_for(guild.id):
            try:
                ledger_id = await asyncio.to_thread(_active_ledger_id, guild.id)
                actor_name, officer_name, caller_name = await asyncio.gather(
                    asyncio.to_thread(
                        _resolve_member_local_name,
                        ledger_id,
                        actor,
                    ),
                    asyncio.to_thread(
                        _resolve_member_local_name,
                        ledger_id,
                        effective_officer,
                    ),
                    asyncio.to_thread(
                        _resolve_member_local_name,
                        ledger_id,
                        caller,
                    ),
                )
                result = await asyncio.to_thread(
                    local_repository.apply_lootsplit,
                    ledger_id,
                    participant_list,
                    amount,
                    battleboard_ids=battle_id_list,
                    actor_discord_user_id=actor.id,
                    actor_name=actor_name,
                    officer_discord_user_id=effective_officer.id,
                    officer_name=officer_name,
                    content_name=content_name,
                    caller_discord_user_id=caller.id,
                    caller_name=caller_name,
                    idempotency_key=(
                        f"discord:lootsplit:{interaction.id}"
                        if getattr(interaction, "id", None)
                        else None
                    ),
                )
            except Exception:
                LOGGER.exception("Local lootsplit failed in guild %s", guild.id)
                await interaction.followup.send("Failed to process the lootsplit. Try again.")
                return

        lines = [f"Lootsplit for **{content_name}**:"]
        if result.credits:
            lines.extend(f"<@{credit.discord_user_id}>: {amount};" for credit in result.credits)
        else:
            lines.append("No participants were processed successfully.")
        if result.missing_nicknames:
            lines.extend(["", f"Missing players: **{', '.join(result.missing_nicknames)}**"])
        await send_followup_lines(
            interaction,
            lines,
            allowed_mentions=allowed_user_mentions(
                credit.discord_user_id for credit in result.credits
            ),
        )

    @app_commands.command(name="bal", description="Get silver balance (yours by default)")
    @app_commands.guild_only()
    async def balance(
        interaction: discord.Interaction,
        member: Optional[discord.Member] = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used inside a server.",
                ephemeral=True,
            )
            return
        if not await _ensure_local_ledger_ready(interaction):
            return
        retry_after = _balance_lookup_cooldown.claim((interaction.guild.id, interaction.user.id))
        if retry_after:
            await interaction.response.send_message(
                f"Please wait {max(1, int(retry_after) + 1)} seconds before checking another balance.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(thinking=True)
        target = member or interaction.user
        try:
            embed = await _load_balance_embed(interaction.guild.id, target)
        except Exception:
            LOGGER.exception(
                "Local balance read failed in guild %s",
                interaction.guild.id,
            )
            await interaction.followup.send("Failed to read balance. Try again.")
            return
        if embed is None:
            await interaction.followup.send("Balance not found.")
            return
        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="get-negative-siphon",
        description="Mention users with negative Siphon balance",
    )
    @app_commands.guild_only()
    async def get_negative_siphon(interaction: discord.Interaction) -> None:
        guild = interaction.guild
        actor = interaction.user
        if guild is None or not isinstance(actor, discord.Member):
            await interaction.response.send_message(
                "This command can only be used inside a server.",
                ephemeral=True,
            )
            return
        if not await _has_economy_access(actor, guild.id):
            await interaction.response.send_message(
                "You don't have permission to use this command.",
                ephemeral=True,
            )
            return
        if not await _ensure_local_ledger_ready(interaction):
            return

        await interaction.response.defer(thinking=True)
        credentials_info = await asyncio.to_thread(
            credential_store.get_credentials_info,
            guild.id,
        )
        if not credentials_info:
            await interaction.followup.send(
                "Siphon requires an optional Google Sheet link. Ask an admin to run **/bot-link-google-sheet**."
            )
            return
        sync_result = await google_sync.refresh_siphon(guild.id, flush_pending=True)
        if not sync_result.success:
            await interaction.followup.send(
                "Siphon could not be refreshed from Google Sheets: " + sync_result.message
            )
            return

        async with guild_lifecycle.lock_for(guild.id):
            ledger_id = await asyncio.to_thread(_active_ledger_id, guild.id)
            negative_players = await asyncio.to_thread(
                local_repository.list_negative_siphon,
                ledger_id,
                max_age_seconds=google_sync.SIPHON_STALE_AFTER_SECONDS,
            )
        if not negative_players:
            await interaction.followup.send("No users have a negative Siphon balance.")
            return
        lines = ["## Users with negative Siphon :oil: balance:"]
        lines.extend(
            f"<@{player.discord_user_id}>: **{player.siphon:,}**"
            for player in negative_players
            if player.siphon is not None
        )
        await send_followup_lines(
            interaction,
            lines,
            allowed_mentions=allowed_user_mentions(
                player.discord_user_id for player in negative_players
            ),
        )

    @app_commands.command(name="bal-add", description="Add silver balance to a player")
    @app_commands.guild_only()
    async def balance_add(
        interaction: discord.Interaction,
        member: discord.Member,
        add_silver: str,
        reason: str = "Manual",
    ) -> None:
        await _handle_balance_change(
            interaction,
            member,
            add_silver,
            reason,
            option_name="add_silver",
            remove=False,
        )

    @app_commands.command(name="bal-remove", description="Remove silver balance from a player")
    @app_commands.guild_only()
    async def balance_remove(
        interaction: discord.Interaction,
        member: discord.Member,
        remove_silver: str,
        reason: str = "Payout",
    ) -> None:
        await _handle_balance_change(
            interaction,
            member,
            remove_silver,
            reason,
            option_name="remove_silver",
            remove=True,
        )

    return [lootsplit, balance, get_negative_siphon, balance_add, balance_remove]


def create_prefix_economy_commands(
    bot: "RealmProtectorBot",
) -> list[commands.Command]:
    @commands.command(name="bal")
    @commands.guild_only()
    async def balance(ctx: commands.Context) -> None:
        """Show the invoking user's local balance."""

        guild = ctx.guild
        if guild is None:
            await ctx.send("This command can only be used inside a server.")
            return

        unavailable_message = await asyncio.to_thread(
            _local_ledger_unavailable_message,
            guild.id,
        )
        if unavailable_message:
            await ctx.send(unavailable_message)
            return

        retry_after = _balance_lookup_cooldown.claim((guild.id, ctx.author.id))
        if retry_after:
            await ctx.send(
                f"Please wait {max(1, int(retry_after) + 1)} seconds before checking another balance."
            )
            return

        try:
            embed = await _load_balance_embed(guild.id, ctx.author)
        except Exception:
            LOGGER.exception("Local balance read failed in guild %s", guild.id)
            await ctx.send("Failed to read balance. Try again.")
            return
        if embed is None:
            await ctx.send("Balance not found.")
            return

        await ctx.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="lb")
    @commands.guild_only()
    async def leaderboard(ctx: commands.Context) -> None:
        """Post the current local Silver leaderboard."""

        guild = ctx.guild
        if guild is None:
            await ctx.send("This command can only be used inside a server.")
            return

        unavailable_message = await asyncio.to_thread(
            _local_ledger_unavailable_message,
            guild.id,
        )
        if unavailable_message:
            await ctx.send(unavailable_message)
            return

        try:
            async with guild_lifecycle.lock_for(guild.id):
                ledger_id = await asyncio.to_thread(_active_ledger_id, guild.id)
                page = await asyncio.to_thread(
                    local_repository.get_silver_leaderboard,
                    ledger_id,
                    limit=LEADERBOARD_PAGE_SIZE,
                    offset=0,
                )
        except Exception:
            LOGGER.exception("Leaderboard read failed in guild %s", guild.id)
            await ctx.send("Failed to read the leaderboard. Try again.")
            return

        total_pages = _leaderboard_total_pages(page.total_players)
        await ctx.send(
            embed=_build_leaderboard_embed(page, page_number=1),
            view=LeaderboardView(1, total_pages),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    return [balance, leaderboard]


__all__ = [
    "LeaderboardView",
    "create_economy_commands",
    "create_prefix_economy_commands",
    "register_economy_persistent_views",
]
