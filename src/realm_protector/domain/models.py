from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class LeaveAction(str, Enum):
    """Supported actions when a registered player leaves the Albion guild."""

    KICK = "kick"
    REMOVE_ROLES = "remove_roles"
    NONE = "none"


@dataclass(frozen=True)
class GuildConfiguration:
    """Validated, immutable configuration snapshot for one Discord guild."""

    discord_server_id: int
    target_guild_name: str
    member_role_name: str
    caller_role_names: tuple[str, ...]
    economy_manager_role_names: tuple[str, ...]
    leave_action: LeaveAction
    member_role_id: int | None = None
    caller_role_ids: tuple[int, ...] = ()
    economy_manager_role_ids: tuple[int, ...] = ()
    bot_configuration_channel_id: int | None = None
    bot_configuration_message_id: int | None = None
    bot_updates_channel_id: int | None = None
    utc_timer_guild_name: str | None = None


@dataclass(frozen=True)
class BattleParticipantsResult:
    """Participants from a set of Albion battle identifiers."""

    requested_ids: tuple[str, ...]
    participant_names: tuple[str, ...]
    failed_ids: tuple[str, ...]
