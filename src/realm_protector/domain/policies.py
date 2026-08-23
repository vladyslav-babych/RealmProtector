from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Optional

from src.realm_protector.domain.models import LeaveAction


def normalized_name(value: object) -> str:
    """Normalize an external identity for exact, case-insensitive comparison."""

    return str(value or "").strip().casefold()


def names_match(left: object, right: object) -> bool:
    left_name = normalized_name(left)
    return bool(left_name) and left_name == normalized_name(right)


def select_exact_named_record(
    records: Iterable[Mapping[str, Any]],
    requested_name: str,
    *,
    name_key: str = "Name",
) -> Optional[Mapping[str, Any]]:
    """Return the first exact identity match, never an API relevance guess."""

    requested = normalized_name(requested_name)
    if not requested:
        return None

    for record in records:
        if normalized_name(record.get(name_key)) == requested:
            return record
    return None


def is_in_target_guild(player_guild: object, target_guild: object) -> bool:
    return names_match(player_guild, target_guild)


def coerce_leave_action(
    value: object,
    *,
    default: LeaveAction = LeaveAction.REMOVE_ROLES,
) -> LeaveAction:
    try:
        return LeaveAction(str(value or "").strip().lower())
    except ValueError:
        return default
