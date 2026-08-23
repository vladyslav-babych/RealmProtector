"""Authoritative local registration and recoverable Discord side effects."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import discord

from src.realm_protector.domain.models import GuildConfiguration
from src.realm_protector.domain.policies import is_in_target_guild
from src.realm_protector.infrastructure import (
    albion_api,
    external_io,
    guild_settings,
    local_repository,
    runtime_state,
)
from src.realm_protector.services import guild_lifecycle
from src.realm_protector.services.keyed_locks import KeyedLockPool
from src.realm_protector.services.role_security import self_assignment_error

LOGGER = logging.getLogger(__name__)
_SIDE_EFFECT_RUNTIME_KIND = "registration_side_effect"
_registration_locks: KeyedLockPool[tuple[int, int]] = KeyedLockPool()


async def _commit_registration(
    guild_id: int,
    discord_id: int,
    player_name: str,
    albion_player_id: Optional[str] = None,
) -> local_repository.RegistrationResult:
    ledger_id = await asyncio.to_thread(
        local_repository.get_active_ledger_id,
        guild_id,
    )
    if ledger_id is None:
        raise RuntimeError("No active local ledger is configured for this server.")
    return await asyncio.to_thread(
        local_repository.register_player,
        ledger_id,
        discord_id,
        player_name,
        albion_player_id,
    )


async def sync_discord_nickname(
    member: discord.Member,
    albion_nickname: str,
) -> bool:
    if getattr(member, "nick", None) == albion_nickname:
        return True
    try:
        await member.edit(
            nick=albion_nickname,
            reason="Sync nickname after registration",
        )
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


async def add_member_role(member: discord.Member, role: discord.Role) -> bool:
    role_id = getattr(role, "id", None)
    if any(
        existing is role or (role_id is not None and getattr(existing, "id", None) == role_id)
        for existing in getattr(member, "roles", ())
    ):
        return True
    try:
        await member.add_roles(
            role,
            reason=f"Add {role.name} role after registration",
        )
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


async def _get_player_profile_with_retries(
    player_id: str,
    target_guild_name: Optional[str] = None,
) -> Optional[dict]:
    """Load by stable ID and briefly tolerate Albion membership propagation."""

    fallback_delays = (0.0, 0.75, 1.5)
    next_delay = 0.0
    last_profile: Optional[dict] = None
    for attempt in range(len(fallback_delays)):
        if next_delay:
            await asyncio.sleep(next_delay)
        try:
            profile = await external_io.run_albion(
                albion_api.get_player_profile_by_id,
                player_id,
            )
        except albion_api.AlbionNotFoundError:
            return None
        except albion_api.AlbionTransientError as error:
            if attempt == len(fallback_delays) - 1:
                raise
            retry_after = error.retry_after_seconds
            next_delay = min(
                max(
                    retry_after if retry_after is not None else fallback_delays[attempt + 1],
                    0.0,
                ),
                5.0,
            )
            continue

        if not isinstance(profile, dict):
            raise albion_api.AlbionResponseError("Albion player profile response is invalid.")
        response_id = str(profile.get("Id") or "").strip()
        if response_id and response_id != player_id:
            raise albion_api.AlbionResponseError(
                "Albion returned a profile for a different player ID."
            )
        last_profile = dict(profile)
        profile_guild = str(last_profile.get("GuildName") or "").strip()
        if (
            not target_guild_name
            or is_in_target_guild(profile_guild, target_guild_name)
            or attempt == len(fallback_delays) - 1
        ):
            return last_profile
        next_delay = fallback_delays[attempt + 1]
    return last_profile


async def _get_registered_player_profile_with_retries(
    player: local_repository.PlayerRecord,
    target_guild_name: str,
) -> Optional[dict]:
    """Recheck a stored registration by stable ID or exact legacy nickname."""

    player_id = str(player.albion_player_id or "").strip()
    if player_id:
        return await _get_player_profile_with_retries(player_id, target_guild_name)

    fallback_delays = (0.0, 0.75, 1.5)
    next_delay = 0.0
    last_profile: Optional[dict] = None
    for attempt in range(len(fallback_delays)):
        if next_delay:
            await asyncio.sleep(next_delay)
        try:
            profile = await external_io.run_albion(
                albion_api.get_player_by_nickname,
                player.nickname,
            )
        except albion_api.AlbionNotFoundError:
            return None
        except albion_api.AlbionTransientError as error:
            if attempt == len(fallback_delays) - 1:
                raise
            retry_after = error.retry_after_seconds
            next_delay = min(
                max(
                    retry_after if retry_after is not None else fallback_delays[attempt + 1],
                    0.0,
                ),
                5.0,
            )
            continue
        if profile is None:
            return None
        if not isinstance(profile, dict):
            raise albion_api.AlbionResponseError("Albion player response is invalid.")
        response_name = str(profile.get("Name") or "").strip()
        if response_name.casefold() != player.nickname.strip().casefold():
            raise albion_api.AlbionResponseError(
                "Albion returned a profile for a different nickname."
            )
        last_profile = dict(profile)
        if (
            is_in_target_guild(last_profile.get("GuildName"), target_guild_name)
            or attempt == len(fallback_delays) - 1
        ):
            return last_profile
        next_delay = fallback_delays[attempt + 1]
    return last_profile


def _intent_external_id(discord_id: int, albion_player_id: str) -> str:
    return f"{discord_id}:{albion_player_id}"


async def _save_side_effect_intent(
    guild_id: int,
    external_id: str,
    payload: dict[str, Any],
    *,
    status: str = "pending",
) -> None:
    await asyncio.to_thread(
        runtime_state.upsert_record,
        _SIDE_EFFECT_RUNTIME_KIND,
        guild_id,
        external_id,
        payload,
        status=status,
    )


async def _delete_side_effect_intent(guild_id: int, external_id: str) -> None:
    await asyncio.to_thread(
        runtime_state.delete_record,
        _SIDE_EFFECT_RUNTIME_KIND,
        guild_id,
        external_id,
    )


async def _apply_registration_side_effects(
    member: discord.Member,
    role: discord.Role,
    player_name: str,
) -> tuple[bool, bool]:
    nickname_updated = await sync_discord_nickname(member, player_name)
    role_added = await add_member_role(member, role)
    return nickname_updated, role_added


async def _record_side_effect_result(
    guild_id: int,
    external_id: str,
    payload: dict[str, Any],
    *,
    role_added: bool,
) -> None:
    if role_added:
        await _delete_side_effect_intent(guild_id, external_id)
        return

    next_payload = dict(payload)
    next_payload.pop("nickname_synced", None)
    next_payload.update(
        {
            "role_assigned": False,
            "attempts": int(payload.get("attempts") or 0) + 1,
        }
    )
    await _save_side_effect_intent(guild_id, external_id, next_payload)


def _resolve_member_role(
    guild: discord.Guild,
    configuration: GuildConfiguration,
) -> Optional[discord.Role]:
    role_id = configuration.member_role_id
    if role_id is not None:
        return guild.get_role(role_id)
    return discord.utils.get(guild.roles, name=configuration.member_role_name)


async def force_register_member(
    guild: discord.Guild,
    member: discord.Member,
) -> str:
    """Reverify and reactivate one existing local registration without data loss."""

    configuration = guild_settings.get_configuration(guild.id)
    if configuration is None:
        return "This server is not configured yet. Run **/bot-setup** first."
    expected_generation = guild_lifecycle.generation(guild.id)
    ledger_id = await asyncio.to_thread(
        local_repository.get_active_ledger_id,
        guild.id,
        create_if_missing=False,
    )
    if ledger_id is None:
        return "No active local ledger is configured for this server."
    player = await asyncio.to_thread(
        local_repository.get_player,
        ledger_id,
        member.id,
    )
    if player is None:
        return f"{member.mention} is not registered. Ask them to use **/register** first."

    try:
        profile = await _get_registered_player_profile_with_retries(
            player,
            configuration.target_guild_name,
        )
    except albion_api.AlbionAPIError:
        LOGGER.warning(
            "Force registration could not verify user %s in guild %s",
            member.id,
            guild.id,
            exc_info=True,
        )
        return "Albion is temporarily unavailable, so the registration was not changed."
    if profile is None:
        return (
            f"The stored Albion character **{player.nickname}** could not be found. "
            "The registration was not changed."
        )

    reported_guild = str(profile.get("GuildName") or "").strip()
    if not is_in_target_guild(reported_guild, configuration.target_guild_name):
        location = f"**{reported_guild}**" if reported_guild else "no Albion guild"
        return (
            f"**{player.nickname}** is currently reported in {location}, not "
            f"**{configuration.target_guild_name}**. The registration was not changed."
        )

    verified_player_id = str(profile.get("Id") or player.albion_player_id or "").strip()
    external_id = _intent_external_id(
        member.id,
        verified_player_id or f"nickname:{player.nickname.casefold()}",
    )
    intent_payload: dict[str, Any] = {
        "discord_user_id": member.id,
        "albion_player_id": verified_player_id,
        "nickname": player.nickname,
        "attempts": 0,
        "forced": True,
    }

    async with guild_lifecycle.lock_for(guild.id):
        current_configuration = guild_settings.get_configuration(guild.id)
        current_ledger_id = await asyncio.to_thread(
            local_repository.get_active_ledger_id,
            guild.id,
            create_if_missing=False,
        )
        if (
            current_configuration is None
            or current_ledger_id != ledger_id
            or not guild_lifecycle.is_current(guild.id, expected_generation)
            or current_configuration.target_guild_name.strip().casefold()
            != configuration.target_guild_name.strip().casefold()
        ):
            return "The server configuration changed during verification. Run the command again."

        member_role = _resolve_member_role(guild, current_configuration)
        role_error = self_assignment_error(member_role, guild)
        if member_role is None or role_error:
            return (
                f"Member role configuration error: {role_error or 'role not found'}. "
                "Update the configured Member role and try again."
            )

        async with _registration_locks.hold((guild.id, member.id)):
            current_player = await asyncio.to_thread(
                local_repository.get_player,
                ledger_id,
                member.id,
            )
            if current_player is None:
                return f"{member.mention} is no longer registered."
            if (
                current_player.albion_player_id
                and player.albion_player_id
                and current_player.albion_player_id != player.albion_player_id
            ):
                return "The stored Albion registration changed during verification. Run again."

            await _save_side_effect_intent(guild.id, external_id, intent_payload)
            registration_result = await asyncio.to_thread(
                local_repository.register_player,
                ledger_id,
                member.id,
                current_player.nickname,
                verified_player_id or current_player.albion_player_id,
            )
            if registration_result.status in {
                local_repository.RegistrationStatus.NICKNAME_CONFLICT,
                local_repository.RegistrationStatus.ALBION_ID_CONFLICT,
            }:
                await _delete_side_effect_intent(guild.id, external_id)
                return "The stored character conflicts with another local registration."
            canonical_player = registration_result.player
            if canonical_player is None:
                raise RuntimeError("Force registration completed without a player record.")

            intent_payload.update(
                {
                    "albion_player_id": canonical_player.albion_player_id or verified_player_id,
                    "nickname": canonical_player.nickname,
                    "registration_status": registration_result.status.value,
                }
            )
            await _save_side_effect_intent(guild.id, external_id, intent_payload)
            nickname_updated, role_added = await _apply_registration_side_effects(
                member,
                member_role,
                canonical_player.nickname,
            )
            await _record_side_effect_result(
                guild.id,
                external_id,
                intent_payload,
                role_added=role_added,
            )

    message = (
        f"{member.mention}'s character **{canonical_player.nickname}** was verified in "
        f"**{configuration.target_guild_name}** and is now marked **in guild**."
    )
    warnings = []
    if not nickname_updated:
        warnings.append("I could not update the Discord nickname.")
    if not role_added:
        warnings.append("Member-role repair is pending.")
    if warnings:
        message += "\n" + " ".join(warnings)
    return message


async def register_user(
    context: Any,
    nickname: str,
    albion_player_id: str,
    target_guild_name: Optional[str],
    expected_generation: Optional[int] = None,
    *,
    selected_profile: Optional[dict] = None,
) -> None:
    """Register the explicitly selected Albion ID, then reconcile Discord state."""

    if not target_guild_name:
        await context.send(
            "This server is not configured yet. Ask an admin to run **/bot-setup** first."
        )
        return

    player_id = str(albion_player_id or "").strip()
    if not player_id:
        await context.send("That character selection is invalid. Run **/register** again.")
        return
    if selected_profile is not None:
        selected_id = str(selected_profile.get("Id") or "").strip()
        if selected_id and selected_id != player_id:
            await context.send("That character selection is invalid. Run **/register** again.")
            return

    try:
        player_info = await _get_player_profile_with_retries(
            player_id,
            target_guild_name,
        )
    except albion_api.AlbionAPIError:
        LOGGER.warning(
            "Registration could not verify Albion player ID %s",
            player_id,
            exc_info=True,
        )
        await context.send(
            "Albion is temporarily unavailable, so I could not verify that character. "
            "Please try again later."
        )
        return
    if player_info is None:
        await context.send(
            f"Character **{nickname}** is no longer available. Run **/register** again."
        )
        return

    discord_id = int(context.author.id)
    player_name = str(player_info.get("Name") or nickname).strip()
    player_guild = str(player_info.get("GuildName") or "").strip()

    if not is_in_target_guild(player_guild, target_guild_name):
        if player_guild:
            await context.send(
                f"Character **{player_name}** is in **{player_guild}**.\n"
                f"Only **{target_guild_name}** members can register."
            )
            return
        await context.send(
            f"Character **{player_name}** is not in a guild.\n"
            f"Only **{target_guild_name}** members can register."
        )
        return

    guild_id = int(context.guild.id)
    lifecycle_generation = (
        guild_lifecycle.generation(guild_id) if expected_generation is None else expected_generation
    )
    external_id = _intent_external_id(discord_id, player_id)
    intent_payload: dict[str, Any] = {
        "discord_user_id": discord_id,
        "albion_player_id": player_id,
        "nickname": player_name,
        "attempts": 0,
    }

    async with guild_lifecycle.lock_for(guild_id):
        configuration = guild_settings.get_configuration(guild_id)
        if (
            configuration is None
            or not guild_lifecycle.is_current(guild_id, lifecycle_generation)
            or configuration.target_guild_name.strip().casefold()
            != target_guild_name.strip().casefold()
        ):
            await context.send(
                "The server configuration changed while registration was in "
                "progress. Run **/register** again."
            )
            return

        member_role = _resolve_member_role(context.guild, configuration)
        role_error = self_assignment_error(member_role, context.guild)
        if member_role is None or role_error:
            await context.send(
                f"Member role configuration error: {role_error} "
                "Ask an admin to update the Member role."
            )
            return

        async with _registration_locks.hold((guild_id, discord_id)):
            # Persist the recovery intent before the authoritative local write.
            # A crash at any later point can therefore be repaired on startup.
            await _save_side_effect_intent(
                guild_id,
                external_id,
                intent_payload,
            )
            result = await _commit_registration(
                guild_id,
                discord_id,
                player_name,
                player_id,
            )

            if result.status in {
                local_repository.RegistrationStatus.NICKNAME_CONFLICT,
                local_repository.RegistrationStatus.ALBION_ID_CONFLICT,
            }:
                await _delete_side_effect_intent(guild_id, external_id)
                await context.send(f"Character **{player_name}** is already registered.")
                return

            canonical_player = result.player
            if canonical_player is None:
                await _delete_side_effect_intent(guild_id, external_id)
                raise RuntimeError("Registration completed without a player record.")
            player_name = canonical_player.nickname
            canonical_id = canonical_player.albion_player_id or player_id
            intent_payload.update(
                {
                    "albion_player_id": canonical_id,
                    "nickname": player_name,
                    "registration_status": result.status.value,
                }
            )
            await _save_side_effect_intent(guild_id, external_id, intent_payload)
            nickname_updated, role_added = await _apply_registration_side_effects(
                context.author,
                member_role,
                player_name,
            )
            await _record_side_effect_result(
                guild_id,
                external_id,
                intent_payload,
                role_added=role_added,
            )

    if result.status == local_repository.RegistrationStatus.ALREADY_REGISTERED:
        message = "You are already registered."
    elif result.status == local_repository.RegistrationStatus.REACTIVATED:
        message = (
            f"Your registration was updated and **{player_name}** is marked as **in guild** again."
        )
    else:
        message = f"**{player_name}** was registered successfully."
    warnings = []
    if not nickname_updated:
        warnings.append("I could not update your Discord nickname.")
    if not role_added:
        warnings.append("I could not add the configured Member role; I will retry after restart.")
    if warnings:
        message += "\n" + " ".join(warnings)
    await context.send(message)


async def _mark_reconciliation_pending(
    record: runtime_state.RuntimeRecord,
    message: str,
) -> None:
    payload = dict(record.payload)
    payload["last_error"] = message
    payload["attempts"] = int(payload.get("attempts") or 0) + 1
    await _save_side_effect_intent(
        record.guild_id,
        record.external_id,
        payload,
    )


async def _reconcile_registration_record(
    bot: discord.Client,
    record: runtime_state.RuntimeRecord,
) -> None:
    guild = bot.get_guild(record.guild_id)
    if guild is None:
        await _mark_reconciliation_pending(record, "Discord server is unavailable.")
        return

    try:
        discord_id = int(record.payload.get("discord_user_id") or 0)
    except (TypeError, ValueError):
        discord_id = 0
    if discord_id <= 0:
        await _delete_side_effect_intent(record.guild_id, record.external_id)
        return

    async with guild_lifecycle.lock_for(guild.id):
        ledger_id = await asyncio.to_thread(
            local_repository.get_active_ledger_id,
            guild.id,
            create_if_missing=False,
        )
        if ledger_id is None:
            await _delete_side_effect_intent(record.guild_id, record.external_id)
            return
        player = await asyncio.to_thread(
            local_repository.get_player,
            ledger_id,
            discord_id,
        )
        if player is None or not player.is_active:
            await _delete_side_effect_intent(record.guild_id, record.external_id)
            return

        intended_albion_id = str(record.payload.get("albion_player_id") or "").strip()
        if (
            intended_albion_id
            and player.albion_player_id
            and intended_albion_id != player.albion_player_id
        ):
            await _delete_side_effect_intent(record.guild_id, record.external_id)
            return

        configuration = guild_settings.get_configuration(guild.id)
        if configuration is None:
            await _delete_side_effect_intent(record.guild_id, record.external_id)
            return
        member_role = _resolve_member_role(guild, configuration)
        role_error = self_assignment_error(member_role, guild)
        if member_role is None or role_error:
            await _mark_reconciliation_pending(
                record,
                f"Member role configuration error: {role_error}",
            )
            return

        member = guild.get_member(discord_id)
        if member is None:
            try:
                member = await guild.fetch_member(discord_id)
            except discord.NotFound:
                await _mark_reconciliation_pending(
                    record,
                    "Discord member is not currently in the server.",
                )
                return
            except (discord.Forbidden, discord.HTTPException):
                await _mark_reconciliation_pending(
                    record,
                    "Discord member lookup failed.",
                )
                return

        role_added = await add_member_role(member, member_role)
        await _record_side_effect_result(
            record.guild_id,
            record.external_id,
            dict(record.payload),
            role_added=role_added,
        )


async def reconcile_registration_side_effects(bot: discord.Client) -> None:
    """Repair incomplete registration Member-role assignments."""

    records = await asyncio.to_thread(
        runtime_state.list_records,
        _SIDE_EFFECT_RUNTIME_KIND,
        statuses=("pending", "applying"),
    )
    for record in records:
        try:
            await _reconcile_registration_record(bot, record)
        except Exception:
            LOGGER.exception(
                "Registration side-effect reconciliation failed for guild %s record %s",
                record.guild_id,
                record.external_id,
            )


__all__ = [
    "add_member_role",
    "force_register_member",
    "reconcile_registration_side_effects",
    "register_user",
    "sync_discord_nickname",
]
