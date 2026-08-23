from __future__ import annotations

from typing import Iterable

from src.realm_protector.infrastructure import document_store

_TICKETS_NAMESPACE = "tickets"
_REACTION_ROLES_NAMESPACE = "reaction_roles"
_OBJECTIVES_NAMESPACE = "objectives"


def _positive_role_id(raw_role_id: object) -> int | None:
    if isinstance(raw_role_id, bool) or not isinstance(raw_role_id, (int, str)):
        return None
    try:
        role_id = int(raw_role_id)
    except ValueError:
        return None
    return role_id if role_id > 0 else None


def _panel_values(namespace: str, guild_id: int) -> Iterable[tuple[str, dict]]:
    entry = document_store.get_mapping_entry(namespace, guild_id)
    if not isinstance(entry, dict):
        return ()
    panels = entry.get("panels")
    if not isinstance(panels, dict):
        return ()
    return ((str(panel_id), panel) for panel_id, panel in panels.items() if isinstance(panel, dict))


def get_ticket_management_role_sources(guild_id: int) -> dict[int, set[str]]:
    """Return every ticket-management role, including inactive panel tombstones."""

    result: dict[int, set[str]] = {}
    for panel_id, panel in _panel_values(_TICKETS_NAMESPACE, guild_id):
        raw_role_ids = panel.get("management_role_ids")
        if not isinstance(raw_role_ids, list):
            continue
        for raw_role_id in raw_role_ids:
            role_id = _positive_role_id(raw_role_id)
            if role_id is not None:
                result.setdefault(role_id, set()).add(f"ticket management panel {panel_id}")
    return result


def get_reaction_role_sources(guild_id: int) -> dict[int, set[str]]:
    result: dict[int, set[str]] = {}
    for panel_id, panel in _panel_values(_REACTION_ROLES_NAMESPACE, guild_id):
        reactions = panel.get("reactions")
        if not isinstance(reactions, list):
            continue
        for reaction in reactions:
            if not isinstance(reaction, dict):
                continue
            role_id = _positive_role_id(reaction.get("role_id"))
            if role_id is not None:
                result.setdefault(role_id, set()).add(f"reaction panel {panel_id}")
    return result


def get_objective_notification_role_sources(guild_id: int) -> dict[int, set[str]]:
    result: dict[int, set[str]] = {}
    entry = document_store.get_mapping_entry(_OBJECTIVES_NAMESPACE, guild_id)
    if not isinstance(entry, dict):
        return result
    objectives = entry.get("objectives")
    if not isinstance(objectives, list):
        return result
    for index, objective in enumerate(objectives):
        if not isinstance(objective, dict):
            continue
        role_id = _positive_role_id(objective.get("notify_role_id"))
        if role_id is None:
            continue
        objective_id = str(objective.get("id") or index)
        result.setdefault(role_id, set()).add(f"objective notification {objective_id}")
    return result


__all__ = [
    "get_objective_notification_role_sources",
    "get_reaction_role_sources",
    "get_ticket_management_role_sources",
]
