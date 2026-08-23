from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import discord

from src.realm_protector.domain.models import LeaveAction
from src.realm_protector.domain.policies import coerce_leave_action, is_in_target_guild
from src.realm_protector.infrastructure import (
    albion_api,
    external_io,
    guild_settings,
    local_repository,
)
from src.realm_protector.services import google_sync, guild_lifecycle

_CHECK_INTERVAL_SECONDS = 180
_MEMBERSHIP_AUDIT_CONCURRENCY = 4
_DEPARTURE_CONFIRMATION_DELAYS_SECONDS = (0.0, 1.0, 3.0)
_RECENT_REGISTRATION_GRACE_SECONDS = 10 * 60

_tracker_task: Optional[asyncio.Task] = None


def start_guild_member_tracker(bot: discord.Client) -> None:
    global _tracker_task
    if _tracker_task is not None and not _tracker_task.done():
        return
    _tracker_task = asyncio.create_task(
        _tracker_loop(bot),
        name="realm-protector-membership",
    )


async def stop_guild_member_tracker() -> None:
    """Stop the process-owned membership worker during graceful shutdown."""

    global _tracker_task
    task, _tracker_task = _tracker_task, None
    if task is None:
        return
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        logging.exception("Guild member tracker ended with an error during shutdown")


async def _tracker_loop(bot: discord.Client) -> None:
    while True:
        try:
            await _process_all_servers(bot)
        except Exception:
            logging.exception("Guild member tracker tick failed")
        await asyncio.sleep(_CHECK_INTERVAL_SECONDS)


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


async def _process_all_servers(bot: discord.Client) -> None:
    for server_id in guild_settings.get_all_configured_server_ids():
        guild = bot.get_guild(server_id)
        if guild is None:
            continue

        configuration = guild_settings.get_configuration(server_id)
        if configuration is None:
            continue
        if not await asyncio.to_thread(google_sync.is_cutover_ready, server_id):
            logging.warning(
                "Tracker: waiting for the one-time Sheet migration for server %s",
                server_id,
            )
            continue

        target_guild_name = configuration.target_guild_name
        leave_action = configuration.leave_action
        expected_generation = guild_lifecycle.generation(server_id)

        await _process_server_with_local_storage(
            bot,
            guild,
            target_guild_name,
            leave_action,
            expected_generation,
        )


async def _process_server_with_local_storage(
    bot: discord.Client,
    guild: discord.Guild,
    target_guild_name: str,
    leave_action: str | LeaveAction,
    expected_generation: Optional[int] = None,
) -> None:
    """Audit authoritative local registrations; Google is not required."""

    if expected_generation is None:
        expected_generation = guild_lifecycle.generation(guild.id)
    try:
        ledger_id = await asyncio.to_thread(
            local_repository.get_active_ledger_id,
            guild.id,
        )
        if ledger_id is None:
            raise RuntimeError("No active local ledger is configured.")
        players = await asyncio.to_thread(
            local_repository.list_active_players,
            ledger_id,
        )
    except Exception:
        logging.exception(
            "Tracker: failed to read local registrations for server %s",
            guild.id,
        )
        return

    semaphore = asyncio.Semaphore(_MEMBERSHIP_AUDIT_CONCURRENCY)

    async def audit_player(player: local_repository.PlayerRecord) -> None:
        async with semaphore:
            try:
                await _audit_local_player(
                    bot,
                    guild,
                    player,
                    ledger_id,
                    target_guild_name,
                    leave_action,
                    expected_generation,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception(
                    "Tracker: player audit failed for user %s in server %s",
                    player.discord_user_id,
                    guild.id,
                )

    await asyncio.gather(*(audit_player(player) for player in players))


async def _audit_local_player(
    bot: discord.Client,
    guild: discord.Guild,
    player: local_repository.PlayerRecord,
    ledger_id: int,
    target_guild_name: str,
    leave_action: str | LeaveAction,
    expected_generation: int,
) -> None:
    """Persist Albion truth first, then independently enforce it on Discord."""

    if _is_within_registration_grace(player):
        return

    player_id = str(getattr(player, "albion_player_id", None) or "").strip()
    lookup = albion_api.get_player_profile_by_id if player_id else albion_api.get_player_by_nickname
    lookup_key = player_id or player.nickname
    try:
        profile = await _load_profile_with_departure_confirmation(
            lookup,
            lookup_key,
            target_guild_name,
        )
    except albion_api.AlbionAPIError as error:
        logging.info(
            "Tracker: Albion lookup failed for player %s in server %s: %s",
            lookup_key,
            guild.id,
            error,
        )
        return
    except Exception:
        logging.exception(
            "Tracker: unexpected Albion lookup failure for player %s in server %s",
            lookup_key,
            guild.id,
        )
        return
    if not isinstance(profile, dict):
        return
    player_guild = str(profile.get("GuildName") or "").strip()
    if is_in_target_guild(player_guild, target_guild_name):
        return

    async with guild_lifecycle.lock_for(guild.id):
        if not _configuration_still_matches(
            guild.id,
            target_guild_name,
            leave_action,
            expected_generation,
        ):
            return

        try:
            current_player = await asyncio.to_thread(
                local_repository.get_player,
                ledger_id,
                player.discord_user_id,
            )
            if (
                current_player is None
                or not current_player.is_active
                or current_player.revision != player.revision
            ):
                return
            await asyncio.to_thread(
                local_repository.set_in_guild,
                ledger_id,
                player.discord_user_id,
                False,
            )
        except Exception:
            logging.exception(
                "Tracker: failed to persist departed player %s in server %s",
                player.discord_user_id,
                guild.id,
            )
            return

        member = guild.get_member(player.discord_user_id)
        if member is None:
            try:
                member = await guild.fetch_member(player.discord_user_id)
            except discord.NotFound:
                return
            except (discord.Forbidden, discord.HTTPException):
                logging.warning(
                    "Tracker: local departure recorded but Discord member lookup "
                    "failed for user %s in server %s",
                    player.discord_user_id,
                    guild.id,
                )
                return

        try:
            action_succeeded = await _apply_leave_action(
                bot,
                guild,
                member,
                leave_action,
            )
        except Exception:
            logging.exception(
                "Tracker: unexpected Discord enforcement failure for user %s in server %s",
                player.discord_user_id,
                guild.id,
            )
            return
        if not action_succeeded:
            logging.warning(
                "Tracker: local departure recorded but Discord enforcement failed "
                "for user %s in server %s",
                player.discord_user_id,
                guild.id,
            )


def _is_within_registration_grace(player: local_repository.PlayerRecord) -> bool:
    raw_updated_at = str(getattr(player, "updated_at", "") or "").strip()
    if not raw_updated_at:
        return False
    try:
        updated_at = datetime.fromisoformat(raw_updated_at)
    except ValueError:
        return False
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    age_seconds = (datetime.now(timezone.utc) - updated_at.astimezone(timezone.utc)).total_seconds()
    return age_seconds < _RECENT_REGISTRATION_GRACE_SECONDS


async def _load_profile_with_departure_confirmation(
    lookup,
    lookup_key: str,
    target_guild_name: str,
) -> Optional[dict]:
    """Require repeated non-membership responses before enforcing departure."""

    last_profile: Optional[dict] = None
    for delay_seconds in _DEPARTURE_CONFIRMATION_DELAYS_SECONDS:
        if delay_seconds:
            await asyncio.sleep(delay_seconds)
        profile = await external_io.run_albion(lookup, lookup_key)
        if not isinstance(profile, dict):
            return None
        last_profile = dict(profile)
        if is_in_target_guild(last_profile.get("GuildName"), target_guild_name):
            return last_profile
    return last_profile


def _configuration_still_matches(
    guild_id: int,
    target_guild_name: str,
    leave_action: str | LeaveAction,
    expected_generation: int,
) -> bool:
    if not guild_lifecycle.is_current(guild_id, expected_generation):
        return False
    configuration = guild_settings.get_configuration(guild_id)
    if configuration is None:
        return False
    return configuration.target_guild_name.strip().casefold() == (
        target_guild_name or ""
    ).strip().casefold() and coerce_leave_action(configuration.leave_action) == coerce_leave_action(
        leave_action
    )


async def _apply_leave_action(
    bot: discord.Client,
    guild: discord.Guild,
    member: discord.Member,
    leave_action: str | LeaveAction,
) -> bool:
    action = coerce_leave_action(leave_action)
    reason = f"Albion guild membership audit ({_now_utc()})"

    if action == LeaveAction.NONE:
        return True

    if member.id == guild.owner_id or getattr(member, "bot", False):
        return False
    try:
        if member.guild_permissions.administrator:
            return False
    except Exception:
        return False

    if action == LeaveAction.KICK:
        try:
            await member.kick(reason=reason)
        except (discord.Forbidden, discord.HTTPException):
            return False
        return True

    return await _remove_all_roles(bot, guild, member, reason)


async def _remove_all_roles(
    bot: discord.Client,
    guild: discord.Guild,
    member: discord.Member,
    reason: str,
) -> bool:
    me = guild.me or guild.get_member(getattr(bot.user, "id", 0))
    if me is None:
        try:
            me = await guild.fetch_member(getattr(bot.user, "id", 0))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            me = None

    bot_top_role = me.top_role if me is not None else None

    roles_to_remove: list[discord.Role] = []
    has_unmanageable_role = False
    for role in member.roles:
        if role.is_default():
            continue
        if role.managed:
            continue
        if bot_top_role is not None and role >= bot_top_role:
            has_unmanageable_role = True
            continue
        roles_to_remove.append(role)

    if not roles_to_remove:
        return not has_unmanageable_role

    try:
        await member.remove_roles(*roles_to_remove, reason=reason)
    except (discord.Forbidden, discord.HTTPException):
        return False
    return not has_unmanageable_role
