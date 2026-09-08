"""Field-level reaction-panel editing with durable, non-replaying mapping changes."""

from __future__ import annotations

from copy import deepcopy

import discord

from src.realm_protector.bot import reaction_roles
from src.realm_protector.bot.config_editor import ConfigActionsView, ConfigField, RefreshWarnings
from src.realm_protector.infrastructure import guild_settings, runtime_state
from src.realm_protector.services import role_security


def _mappings(values: list[dict]) -> list[dict]:
    return [
        {"emoji": str(item.get("emoji") or ""), "role_id": int(item.get("role_id") or 0)}
        for item in values
    ]


class ReactionConfigurationAdapter:
    kind = "reaction"
    title = "Reaction-role configuration"
    fields: tuple[ConfigField, ...] = (
        ConfigField("panel_name", "Panel title", max_length=256),
        ConfigField("panel_message", "Panel message", max_length=3000),
        ConfigField("reactions", "Emoji and role mappings", kind="reactions"),
        ConfigField("destination_channel_id", "Panel channel", kind="channel"),
    )

    def keys(self, guild_id: int) -> list[str]:
        if not guild_settings.get_target_guild(guild_id):
            return []
        return [str(panel["id"]) for panel in reaction_roles._list_panels(guild_id)]

    def resolve_key(self, guild_id: int, message_id: int) -> str | None:
        panel = reaction_roles._get_panel_by_message_id(guild_id, message_id)
        if panel is not None:
            return str(panel["id"])
        return next(
            (
                str(candidate["id"])
                for candidate in reaction_roles._list_panels(guild_id)
                if int(candidate.get("configuration_message_id") or 0) == message_id
            ),
            None,
        )

    def load(self, guild_id: int, key: str) -> dict | None:
        if not guild_settings.get_target_guild(guild_id):
            return None
        panel = reaction_roles._get_panel_by_id(guild_id, key)
        if panel is None:
            return None
        return {
            "panel_name": str(panel.get("panel_name") or "Roles"),
            "panel_message": str(panel.get("panel_message") or ""),
            "reactions": _mappings(panel.get("reactions", [])),
            "destination_channel_id": int(
                panel.get("destination_channel_id") or panel.get("panel_channel_id") or 0
            ),
        }

    async def validate(self, guild: discord.Guild, key: str, values: dict) -> None:
        existing = self.load(guild.id, key)
        if existing is None:
            raise ValueError("This reaction-role panel is no longer configured.")
        title = str(values.get("panel_name") or "").strip()
        message = str(values.get("panel_message") or "").strip()
        if not title or len(title) > 256 or not message or len(message) > 3000:
            raise ValueError("Set a title (1-256 characters) and message (1-3000 characters).")
        mappings = values.get("reactions")
        if not isinstance(mappings, list) or not 1 <= len(mappings) <= 6:
            raise ValueError("Configure between one and six emoji/role mappings.")
        seen: set[str] = set()
        seen_roles: set[int] = set()
        for mapping in mappings:
            normalized = reaction_roles._normalize_emoji_input(str(mapping.get("emoji") or ""))
            if not normalized:
                raise ValueError("Each mapping needs one supported Unicode emoji.")
            emoji_key = reaction_roles._emoji_key(normalized)
            if emoji_key in seen:
                raise ValueError("Each emoji can be used only once in a panel.")
            seen.add(emoji_key)
            role_id = int(mapping.get("role_id") or 0)
            if role_id in seen_roles:
                raise ValueError("Each role can be used only once in a panel.")
            seen_roles.add(role_id)
            role = guild.get_role(role_id)
            error = role_security.self_assignment_error(role, guild)
            if error:
                raise ValueError(error)
        channel = await self._channel(guild, int(values.get("destination_channel_id") or 0))
        if guild.me is None:
            raise ValueError("Bot member information is unavailable. Try again.")
        permissions = channel.permissions_for(guild.me)
        if not (
            permissions.view_channel
            and permissions.send_messages
            and permissions.embed_links
            and permissions.add_reactions
            and permissions.read_message_history
        ):
            raise ValueError(
                "I need View Channel, Send Messages, Embed Links, Add Reactions, "
                "and Read Message History in the panel channel."
            )
        if existing["reactions"] != _mappings(mappings) or existing[
            "destination_channel_id"
        ] != int(values["destination_channel_id"]):
            panel = reaction_roles._get_panel_by_id(guild.id, key)
            assert panel is not None
            try:
                old_channel = await self._channel(guild, int(panel.get("panel_channel_id") or 0))
            except discord.NotFound:
                old_channel = None
            if (
                old_channel is not None
                and not old_channel.permissions_for(guild.me).manage_messages
            ):
                raise ValueError(
                    "I need Manage Messages in the current panel channel to safely clear "
                    "old reactions. Existing assigned roles will be kept."
                )

    @staticmethod
    async def _channel(guild: discord.Guild, channel_id: int) -> discord.TextChannel:
        channel = guild.get_channel(channel_id) or await guild.fetch_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise ValueError("Select an existing server text channel.")
        return channel

    def save(self, guild_id: int, key: str, values: dict) -> None:
        panel = reaction_roles._get_panel_by_id(guild_id, key)
        if panel is None:
            raise ValueError("This reaction-role panel is no longer configured.")
        old_mappings = {
            reaction_roles._emoji_key(str(item["emoji"])): item
            for item in panel.get("reactions", [])
        }
        moving = int(panel.get("panel_channel_id") or 0) != int(values["destination_channel_id"])
        reset_emojis = set(panel.get("pending_reaction_resets", []))
        new_mappings = []
        for item in _mappings(values["reactions"]):
            normalized = reaction_roles._normalize_emoji_input(item["emoji"])
            if normalized is None:
                raise ValueError("Invalid reaction emoji.")
            item["emoji"] = normalized
            old = old_mappings.pop(reaction_roles._emoji_key(normalized), None)
            if old is not None and int(old["role_id"]) == item["role_id"] and not moving:
                item = {**deepcopy(old), **item}
            else:
                # A new mapping has no authority over roles assigned before this edit.
                item["tracked_member_ids"] = []
                reset_emojis.add(str(old["emoji"]) if old else normalized)
            new_mappings.append(item)
        reset_emojis.update(str(item["emoji"]) for item in old_mappings.values())
        panel.update(
            panel_name=str(values["panel_name"]).strip(),
            panel_message=str(values["panel_message"]).strip(),
            destination_channel_id=int(values["destination_channel_id"]),
            reactions=new_mappings,
            pending_reaction_resets=sorted(reset_emojis),
        )
        reaction_roles._save_panel(guild_id, panel)

    async def _clean_previous_publications(self, guild: discord.Guild, key: str) -> bool:
        """Finish/compensate an interrupted move before attempting another send."""
        for record in runtime_state.list_records(
            reaction_roles._PUBLISH_RUNTIME_KIND, guild_id=guild.id
        ):
            if str(record.payload.get("panel_id") or "") != key:
                continue
            if reaction_roles._publication_was_committed(record):
                if not await reaction_roles._disable_previous_reaction_panel_message(
                    guild, record.payload.get("panel") or {}
                ):
                    return False
            else:
                message = await reaction_roles._resolve_pending_publish_message(guild, record)
                if message is not None and not await reaction_roles._compensate_publish_message(
                    message
                ):
                    return False
            reaction_roles._finish_publish(guild.id, record.external_id)
        return True

    async def refresh(self, guild: discord.Guild, key: str) -> list[str]:
        panel = reaction_roles._get_panel_by_id(guild.id, key)
        if panel is None or not guild_settings.get_target_guild(guild.id):
            return []
        try:
            if not await self._clean_previous_publications(guild, key):
                return [
                    "Previous reaction-panel publication still needs cleanup.",
                    *await self._refresh_summary(guild, panel),
                ]
            channel = await self._channel(
                guild,
                int(panel.get("destination_channel_id") or panel.get("panel_channel_id") or 0),
            )
            message = None
            if int(panel.get("panel_channel_id") or 0) == channel.id:
                try:
                    message = await channel.fetch_message(int(panel.get("panel_message_id") or 0))
                except discord.NotFound:
                    summary_warnings = await self._refresh_summary(guild, panel)
                    return RefreshWarnings(
                        [
                            "The reaction-role panel message was deleted. Use Send panel again "
                            "in reaction-role setup if you want to replace it.",
                            *summary_warnings,
                        ],
                        retryable=bool(summary_warnings)
                        and getattr(summary_warnings, "retryable", True),
                    )
            if message is None:
                candidate = deepcopy(panel)
                candidate["previous_panel_channel_id"] = int(panel.get("panel_channel_id") or 0)
                candidate["previous_panel_message_id"] = int(panel.get("panel_message_id") or 0)
                candidate.pop("panel_channel_id", None)
                candidate.pop("panel_message_id", None)
                for mapping in candidate.get("reactions", []):
                    mapping["tracked_member_ids"] = []
                candidate["pending_reaction_resets"] = []
                message, operation_id = await reaction_roles._post_pending_panel(
                    guild, channel, candidate, operation="resend"
                )
                try:
                    reaction_roles._save_panel(guild.id, candidate)
                except Exception:
                    await reaction_roles._abort_publish(
                        guild.id, operation_id, candidate, message, operation="resend"
                    )
                    raise
                panel = candidate
                if not await reaction_roles._disable_previous_reaction_panel_message(guild, panel):
                    return [
                        "The new panel is live; disabling its old message will be retried.",
                        *await self._refresh_summary(guild, panel),
                    ]
                reaction_roles._finish_publish(guild.id, operation_id)
            for raw_emoji in tuple(panel.get("pending_reaction_resets", [])):
                await message.clear_reaction(str(raw_emoji))
                panel["pending_reaction_resets"].remove(raw_emoji)
                reaction_roles._save_panel(guild.id, panel)
            await message.edit(
                content=None,
                embed=reaction_roles._build_panel_embed(
                    str(panel["panel_name"]),
                    str(panel["panel_message"]),
                    guild,
                    panel.get("reactions", []),
                ),
                view=ConfigActionsView(self.kind, "current"),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            for mapping in panel.get("reactions", []):
                if not await reaction_roles._add_panel_reaction(message, str(mapping["emoji"])):
                    return [
                        "A reaction could not be added; panel refresh will be retried.",
                        *await self._refresh_summary(guild, panel),
                    ]
            reaction_roles._offline_reconciled_panel_versions.pop((guild.id, message.id), None)
            return await self._refresh_summary(guild, panel)
        except (discord.HTTPException, ValueError, reaction_roles._PanelPublishError) as error:
            return [
                f"Reaction-role panel refresh is pending: {error}",
                *await self._refresh_summary(guild, panel),
            ]

    async def _refresh_summary(self, guild: discord.Guild, panel: dict) -> list[str]:
        channel_id = int(panel.get("configuration_channel_id") or 0)
        message_id = int(panel.get("configuration_message_id") or 0)
        if not channel_id or not message_id:
            return []
        try:
            channel = await self._channel(guild, channel_id)
            message = await channel.fetch_message(message_id)
            await message.edit(
                embed=reaction_roles._build_configuration_embed(guild, panel),
                view=ConfigActionsView(self.kind, "current"),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return []
        except discord.NotFound:
            return RefreshWarnings(
                ["The reaction-role configuration summary was deleted; it was not recreated."],
                retryable=False,
            )
        except (discord.HTTPException, ValueError) as error:
            return [f"Reaction-role configuration summary refresh is pending: {error}"]


ADAPTER = ReactionConfigurationAdapter()
