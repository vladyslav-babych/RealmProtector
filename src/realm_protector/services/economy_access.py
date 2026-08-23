"""Shared authorization policy for economy and Siphon workflows."""

from __future__ import annotations

from typing import Any

from src.realm_protector.infrastructure import guild_settings
from src.realm_protector.services.authorization import is_admin
from src.realm_protector.services.role_security import member_has_safe_privileged_role


async def has_economy_access(member: Any, guild_id: int) -> bool:
    if await is_admin(member):
        return True
    configured_role_ids = guild_settings.get_economy_manager_role_ids(guild_id)
    if configured_role_ids:
        return member_has_safe_privileged_role(
            member,
            role_ids=configured_role_ids,
        )
    return member_has_safe_privileged_role(
        member,
        role_names=guild_settings.get_economy_manager_roles(guild_id),
    )


__all__ = ["has_economy_access"]
