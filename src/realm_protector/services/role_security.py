from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

from src.realm_protector.infrastructure import guild_settings, role_security_store
from src.realm_protector.services.authorization import (
    authorization_role_configuration_error,
    automatic_role_assignment_error,
)


def _normalized_role_name(value: object) -> str:
    return str(value or "").strip().casefold()


def _positive_ids(values: Iterable[object]) -> set[int]:
    result: set[int] = set()
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            continue
        try:
            role_id = int(value)
        except ValueError:
            continue
        if role_id > 0:
            result.add(role_id)
    return result


def _merge_id_sources(
    destination: dict[int, set[str]],
    additions: Mapping[int, Iterable[str]],
) -> None:
    for raw_role_id, sources in additions.items():
        try:
            role_id = int(raw_role_id)
        except (TypeError, ValueError):
            continue
        if role_id <= 0:
            continue
        destination.setdefault(role_id, set()).update(str(source) for source in sources)


@dataclass(frozen=True)
class RoleSecurityState:
    privileged_id_sources: Mapping[int, frozenset[str]] = field(default_factory=dict)
    privileged_name_sources: Mapping[str, frozenset[str]] = field(default_factory=dict)
    self_assignable_id_sources: Mapping[int, frozenset[str]] = field(default_factory=dict)
    self_assignable_name_sources: Mapping[str, frozenset[str]] = field(default_factory=dict)

    @property
    def privileged_ids(self) -> frozenset[int]:
        return frozenset(self.privileged_id_sources)

    @property
    def privileged_legacy_names(self) -> frozenset[str]:
        return frozenset(self.privileged_name_sources)

    @property
    def self_assignable_ids(self) -> frozenset[int]:
        return frozenset(self.self_assignable_id_sources)

    @property
    def self_assignable_legacy_names(self) -> frozenset[str]:
        return frozenset(self.self_assignable_name_sources)

    def extended(
        self,
        *,
        privileged_ids: Iterable[object] = (),
        self_assignable_ids: Iterable[object] = (),
        source: str = "pending configuration",
    ) -> "RoleSecurityState":
        privileged = {
            role_id: set(sources) for role_id, sources in self.privileged_id_sources.items()
        }
        self_assignable = {
            role_id: set(sources) for role_id, sources in self.self_assignable_id_sources.items()
        }
        for role_id in _positive_ids(privileged_ids):
            privileged.setdefault(role_id, set()).add(source)
        for role_id in _positive_ids(self_assignable_ids):
            self_assignable.setdefault(role_id, set()).add(source)
        return RoleSecurityState(
            privileged_id_sources={
                role_id: frozenset(sources) for role_id, sources in privileged.items()
            },
            privileged_name_sources=self.privileged_name_sources,
            self_assignable_id_sources={
                role_id: frozenset(sources) for role_id, sources in self_assignable.items()
            },
            self_assignable_name_sources=self.self_assignable_name_sources,
        )


def collect_role_security_state(guild_id: int) -> RoleSecurityState:
    privileged_ids: dict[int, set[str]] = {}
    privileged_names: dict[str, set[str]] = {}
    self_assignable_ids: dict[int, set[str]] = {}
    self_assignable_names: dict[str, set[str]] = {}

    configuration = guild_settings.get_configuration(guild_id)
    if configuration is not None:
        caller_ids = configuration.caller_role_ids
        if caller_ids:
            for role_id in caller_ids:
                privileged_ids.setdefault(role_id, set()).add("Caller role")
        else:
            for role_name in configuration.caller_role_names:
                normalized = _normalized_role_name(role_name)
                if normalized:
                    privileged_names.setdefault(normalized, set()).add("legacy Caller role")

        economy_ids = configuration.economy_manager_role_ids
        if economy_ids:
            for role_id in economy_ids:
                privileged_ids.setdefault(role_id, set()).add("Economy Manager role")
        else:
            for role_name in configuration.economy_manager_role_names:
                normalized = _normalized_role_name(role_name)
                if normalized:
                    privileged_names.setdefault(normalized, set()).add(
                        "legacy Economy Manager role"
                    )

    _merge_id_sources(
        privileged_ids,
        role_security_store.get_ticket_management_role_sources(guild_id),
    )

    if configuration is not None:
        member_role_id = configuration.member_role_id
        if member_role_id:
            self_assignable_ids.setdefault(member_role_id, set()).add("Member role")
        else:
            member_role_name = _normalized_role_name(configuration.member_role_name)
            if member_role_name:
                self_assignable_names.setdefault(member_role_name, set()).add("legacy Member role")

    _merge_id_sources(
        self_assignable_ids,
        role_security_store.get_reaction_role_sources(guild_id),
    )
    _merge_id_sources(
        self_assignable_ids,
        role_security_store.get_objective_notification_role_sources(guild_id),
    )

    return RoleSecurityState(
        privileged_id_sources={
            role_id: frozenset(sources) for role_id, sources in privileged_ids.items()
        },
        privileged_name_sources={
            role_name: frozenset(sources) for role_name, sources in privileged_names.items()
        },
        self_assignable_id_sources={
            role_id: frozenset(sources) for role_id, sources in self_assignable_ids.items()
        },
        self_assignable_name_sources={
            role_name: frozenset(sources) for role_name, sources in self_assignable_names.items()
        },
    )


def _role_matches(
    role: Any,
    id_sources: Mapping[int, frozenset[str]],
    name_sources: Mapping[str, frozenset[str]],
) -> bool:
    try:
        role_id = int(getattr(role, "id", 0) or 0)
    except (TypeError, ValueError):
        role_id = 0
    if role_id in id_sources:
        return True
    return _normalized_role_name(getattr(role, "name", "")) in name_sources


def role_is_bot_privileged(role: Any, state: RoleSecurityState) -> bool:
    return _role_matches(
        role,
        state.privileged_id_sources,
        state.privileged_name_sources,
    )


def role_is_self_assignable(role: Any, state: RoleSecurityState) -> bool:
    return _role_matches(
        role,
        state.self_assignable_id_sources,
        state.self_assignable_name_sources,
    )


def self_assignment_error(
    role: Any,
    guild: Any,
    state: Optional[RoleSecurityState] = None,
) -> Optional[str]:
    """Validate assignment mechanics without rejecting configured role overlap."""

    return automatic_role_assignment_error(role, guild)


def privileged_assignment_error(
    role: Any,
    guild: Any,
    state: Optional[RoleSecurityState] = None,
) -> Optional[str]:
    """Validate a privileged role without rejecting self-assignment overlap."""

    return authorization_role_configuration_error(role, guild)


def member_has_safe_privileged_role(
    member: Any,
    *,
    role_ids: Optional[Iterable[object]] = None,
    role_names: Optional[Iterable[object]] = None,
    state: Optional[RoleSecurityState] = None,
) -> bool:
    configured_ids = _positive_ids(role_ids or ())
    configured_names = {
        normalized
        for normalized in (_normalized_role_name(name) for name in (role_names or ()))
        if normalized
    }
    member_guild = getattr(member, "guild", None)
    guild_id = getattr(member_guild, "id", None)
    security_state = state or (
        collect_role_security_state(int(guild_id)) if guild_id is not None else RoleSecurityState()
    )

    for role in getattr(member, "roles", ()):
        if authorization_role_configuration_error(role, member_guild):
            continue
        # A real Discord member must match a role source that is currently
        # registered as bot-privileged. This prevents legacy default names such
        # as "Caller" from authorizing commands after the main configuration is
        # removed, while retained ticket tombstones continue to work.
        if guild_id is not None and not role_is_bot_privileged(
            role,
            security_state,
        ):
            continue
        if configured_ids:
            if getattr(role, "id", None) in configured_ids:
                return True
        elif _normalized_role_name(getattr(role, "name", "")) in configured_names:
            return True
    return False


__all__ = [
    "RoleSecurityState",
    "collect_role_security_state",
    "member_has_safe_privileged_role",
    "privileged_assignment_error",
    "role_is_bot_privileged",
    "role_is_self_assignable",
    "self_assignment_error",
]
