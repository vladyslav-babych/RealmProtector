"""Adapter for the server setup and its optional Google Sheets link."""

from __future__ import annotations

from pathlib import Path

import discord

from src.realm_protector.bot.config_editor import ConfigActionsView, ConfigField, RefreshWarnings
from src.realm_protector.infrastructure import credential_store, guild_settings
from src.realm_protector.services import guild_lifecycle, role_security

_LINK_FIELDS = {
    "credentials_file": "credentials_file",
    "google_sheet_name": "google_sheet_name",
    "players_worksheet": "google_worksheet_name",
    "lootsplit_worksheet": "lootsplit_history_worksheet_name",
    "balance_worksheet": "balance_history_worksheet_name",
}


class BotConfigAdapter:
    kind = "bot"
    title = "Bot Configuration"
    fields: tuple[ConfigField, ...] = (
        ConfigField("guild_name", "Guild name", max_length=200),
        ConfigField("caller_roles", "Caller role(s)", kind="roles"),
        ConfigField("economy_roles", "Economy Manager role(s)", kind="roles"),
        ConfigField("member_role", "Member role", kind="role"),
        ConfigField(
            "leave_action",
            "Leave guild action",
            kind="choice",
            options=(
                ("Kick from server", "kick"),
                ("Remove all roles", "remove_roles"),
                ("Do nothing", "none"),
            ),
        ),
        ConfigField("bot_updates_channel_id", "Bot updates channel", kind="channel"),
        ConfigField("credentials_file", "Credentials file", max_length=200),
        ConfigField("google_sheet_name", "Google Sheet name", max_length=200),
        ConfigField("players_worksheet", "Players Worksheet name", max_length=200),
        ConfigField("lootsplit_worksheet", "Lootsplit History Worksheet name", max_length=200),
        ConfigField("balance_worksheet", "Balance History Worksheet name", max_length=200),
    )

    def load(self, guild_id: int, key: str) -> dict | None:
        configuration = guild_settings.get_configuration(guild_id) if key == "main" else None
        if configuration is None:
            return None
        link = credential_store.get_credentials_info(guild_id) or {}
        return {
            "guild_name": configuration.target_guild_name,
            "caller_roles": list(configuration.caller_role_ids),
            "economy_roles": list(configuration.economy_manager_role_ids),
            "member_role": configuration.member_role_id,
            "leave_action": configuration.leave_action.value,
            "bot_updates_channel_id": configuration.bot_updates_channel_id,
            **{
                field: str(link.get(storage_key) or "")
                for field, storage_key in _LINK_FIELDS.items()
            },
            "credentials_file": Path(str(link.get("credentials_file") or "")).name
            if link.get("credentials_file")
            else "",
        }

    def keys(self, guild_id: int) -> list[str]:
        return ["main"] if guild_settings.get_configuration(guild_id) else []

    def resolve_key(self, guild_id: int, message_id: int) -> str | None:
        _, current_id = guild_settings.get_bot_configuration_message(guild_id)
        return "main" if current_id and current_id == message_id else None

    async def validate(self, guild, key: str, values: dict) -> None:
        current = self.load(guild.id, key)
        if current is None:
            raise ValueError("Run /bot-setup before updating this configuration.")
        changed = {
            field.key for field in self.fields if values.get(field.key) != current.get(field.key)
        }
        if "guild_name" in changed:
            existing = guild_settings.get_server_id_by_target_guild(values["guild_name"])
            if existing and int(existing) != guild.id:
                raise ValueError(
                    "This Albion guild is already configured in another Discord server."
                )
        for key, singular in (
            ("caller_roles", False),
            ("economy_roles", False),
            ("member_role", True),
        ):
            if key not in changed:
                continue
            ids = [values[key]] if singular else values[key]
            names = []
            for role_id in ids:
                role = guild.get_role(int(role_id))
                error = (
                    role_security.self_assignment_error
                    if singular
                    else role_security.privileged_assignment_error
                )(role, guild)
                if error:
                    raise ValueError(error)
                names.append(role.name)
            values[f"_{key}_names"] = names
        if "bot_updates_channel_id" in changed:
            channel = guild.get_channel(values["bot_updates_channel_id"])
            if not isinstance(channel, discord.TextChannel):
                raise ValueError("Select an existing text channel for bot updates.")
            permissions = channel.permissions_for(guild.me)
            if not permissions.view_channel or not permissions.send_messages:
                raise ValueError(
                    "The bot needs View Channel and Send Messages in its updates channel."
                )
        if changed & _LINK_FIELDS.keys() and not credential_store.get_credentials_info(guild.id):
            raise ValueError(
                "Google Sheets is optional and not linked yet. Run /bot-link-google-sheet first."
            )

    def save(self, guild_id: int, key: str, values: dict) -> None:
        current = self.load(guild_id, key)
        configuration = guild_settings.get_configuration(guild_id)
        if current is None or configuration is None:
            raise ValueError("This bot configuration was removed.")
        changed = [
            field.key for field in self.fields if current.get(field.key) != values.get(field.key)
        ]
        if len(changed) != 1:
            raise ValueError("Update one configuration point at a time.")
        field = changed[0]
        if field in _LINK_FIELDS:
            success, message = credential_store.update_credentials_link_field(
                guild_id, _LINK_FIELDS[field], values[field]
            )
            if not success:
                raise ValueError(message)
        else:
            member_names = values.get("_member_role_names") or [configuration.member_role_name]
            caller_names = values.get("_caller_roles_names") or configuration.caller_role_names
            economy_names = (
                values.get("_economy_roles_names") or configuration.economy_manager_role_names
            )
            guild_settings.set_target_guild(
                guild_id,
                values["guild_name"],
                member_names[0],
                ", ".join(caller_names),
                ", ".join(economy_names),
                values["leave_action"],
                member_role_id=values["member_role"],
                caller_role_ids=values["caller_roles"],
                economy_manager_role_ids=values["economy_roles"],
                bot_updates_channel_id=values["bot_updates_channel_id"],
            )
        guild_lifecycle.advance(guild_id)

    async def refresh(self, guild, key: str) -> list[str]:
        from src.realm_protector.bot import configuration_panel

        channel_id, message_id = guild_settings.get_bot_configuration_message(guild.id)
        if not channel_id or not message_id:
            return RefreshWarnings(
                [
                    "The bot configuration panel has no saved message location. Run /bot-setup to publish it."
                ],
                retryable=False,
            )
        try:
            channel = await configuration_panel._resolve_publication_channel(guild, channel_id)
            message = await channel.fetch_message(message_id)
            await message.edit(
                embed=configuration_panel._build_bot_configuration_panel(guild),
                view=ConfigActionsView(self.kind, key),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.NotFound:
            return RefreshWarnings(
                ["The bot configuration panel was deleted. Run /bot-setup to publish it again."],
                retryable=False,
            )
        except (discord.HTTPException, TypeError):
            return ["The bot configuration panel could not refresh. Check channel permissions."]
        return []


ADAPTER = BotConfigAdapter()
