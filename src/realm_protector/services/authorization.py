from __future__ import annotations

from typing import Any, Optional

# Self-service roles use an allowlist so permissions added by Discord in a future
# discord.py release fail closed until they are explicitly reviewed.
_SAFE_AUTOMATIC_ROLE_PERMISSIONS = frozenset(
    {
        "add_reactions",
        "attach_files",
        "change_nickname",
        "connect",
        "create_instant_invite",
        "create_private_threads",
        "create_public_threads",
        "embed_links",
        "external_emojis",
        "external_stickers",
        "mention_everyone",
        "priority_speaker",
        "read_message_history",
        "read_messages",
        "request_to_speak",
        "send_messages",
        "send_messages_in_threads",
        "send_polls",
        "send_tts_messages",
        "send_voice_messages",
        "set_voice_channel_status",
        "speak",
        "stream",
        "use_application_commands",
        "use_embedded_activities",
        "use_external_apps",
        "use_external_sounds",
        "use_soundboard",
        "use_voice_activation",
    }
)


def _enabled_permission_names(permissions: Any) -> set[str]:
    if permissions is None:
        return set()
    try:
        values = list(iter(permissions))
    except TypeError:
        if hasattr(permissions, "__dict__"):
            values = list(vars(permissions).items())
        else:
            values = [
                (name, getattr(permissions, name, False))
                for name in dir(permissions)
                if not name.startswith("_")
            ]
    return {str(name) for name, enabled in values if bool(enabled)}


def member_is_admin(member: Any) -> bool:
    """Return Discord's effective Administrator permission for a guild member."""

    guild_permissions = getattr(member, "guild_permissions", None)
    effective_permission = getattr(guild_permissions, "administrator", None)
    if effective_permission is not None:
        return bool(effective_permission)

    # Retain support for lightweight member-like objects used by policy tests.
    return any(
        bool(getattr(getattr(role, "permissions", None), "administrator", False))
        for role in getattr(member, "roles", ())
    )


async def is_admin(member: Any) -> bool:
    """Compatibility async API used by the existing Discord handlers."""

    return member_is_admin(member)


def automatic_role_assignment_error(role: Any, guild: Any) -> Optional[str]:
    """Explain why a role must not be granted by self-service bot workflows."""

    if role is None:
        return "The configured role no longer exists."
    if bool(getattr(role, "is_default", lambda: False)()):
        return "The @everyone role cannot be assigned."
    if bool(getattr(role, "managed", False)):
        return "Integration-managed roles cannot be assigned."

    role_guild = getattr(role, "guild", None)
    if role_guild is not None and getattr(role_guild, "id", None) != getattr(guild, "id", None):
        return "The configured role belongs to another server."

    permissions = getattr(role, "permissions", None)
    unsafe = sorted(_enabled_permission_names(permissions) - _SAFE_AUTOMATIC_ROLE_PERMISSIONS)
    if unsafe:
        labels = [name.replace("_", " ").title() for name in unsafe]
        return "Roles with privileged permissions cannot be self-assigned: " + ", ".join(labels)

    bot_member = getattr(guild, "me", None)
    bot_top_role = getattr(bot_member, "top_role", None)
    try:
        if bot_top_role is not None and role >= bot_top_role:
            return "The role must be below the bot's highest role."
    except TypeError:
        return "The role hierarchy could not be validated."

    is_assignable = getattr(role, "is_assignable", None)
    if callable(is_assignable) and not is_assignable():
        return "The bot cannot assign this role with its current hierarchy."
    return None


def authorization_role_configuration_error(role: Any, guild: Any) -> Optional[str]:
    """Explain why a role is unsafe as a privileged workflow gate."""

    if role is None:
        return "The selected role no longer exists."
    if bool(getattr(role, "is_default", lambda: False)()):
        return "The @everyone role cannot authorize privileged bot actions."
    if bool(getattr(role, "managed", False)):
        return "Integration-managed roles cannot authorize privileged bot actions."

    role_guild = getattr(role, "guild", None)
    if role_guild is not None and getattr(role_guild, "id", None) != getattr(guild, "id", None):
        return "The selected role belongs to another server."
    return None


__all__ = [
    "authorization_role_configuration_error",
    "automatic_role_assignment_error",
    "is_admin",
    "member_is_admin",
]
