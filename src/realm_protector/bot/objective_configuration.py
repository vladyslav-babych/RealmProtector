"""Editable objective-panel presentation and destination, separate from timers."""

from __future__ import annotations

import discord

from src.realm_protector.bot import objectives
from src.realm_protector.bot.config_editor import ConfigField, RefreshWarnings
from src.realm_protector.infrastructure import runtime_state


class ObjectiveConfigurationAdapter:
    kind = "objective"
    title = "Objectives configuration"
    fields: tuple[ConfigField, ...] = (
        ConfigField("panel_title", "Panel title", max_length=256),
        ConfigField("panel_message", "Panel message", max_length=4000, required=False),
        ConfigField("destination_channel_id", "Panel channel", kind="channel"),
    )

    def keys(self, guild_id: int) -> list[str]:
        return ["current"] if self.load(guild_id, "current") is not None else []

    def resolve_key(self, guild_id: int, message_id: int) -> str | None:
        entry = objectives._active_objectives_entry(guild_id)
        if entry is not None and objectives._safe_int(entry.get("panel_message_id")) == message_id:
            return "current"
        return None

    def load(self, guild_id: int, key: str) -> dict | None:
        entry = objectives._active_objectives_entry(guild_id)
        if entry is None or key != "current" or not entry.get("panel_message_id"):
            return None
        return {
            "panel_title": str(entry.get("panel_title") or "Active objectives:"),
            "panel_message": str(entry.get("panel_message") or ""),
            "destination_channel_id": objectives._safe_int(
                entry.get("destination_channel_id") or entry.get("panel_channel_id")
            ),
        }

    async def validate(self, guild: discord.Guild, key: str, values: dict) -> None:
        if self.load(guild.id, key) is None:
            raise ValueError("The objectives panel is no longer configured.")
        title = str(values.get("panel_title") or "").strip()
        if not title or len(title) > 256:
            raise ValueError("The panel title must contain 1-256 characters.")
        if len(str(values.get("panel_message") or "")) > 4000:
            raise ValueError("The panel message must not exceed 4000 characters.")
        channel = await objectives._resolve_text_channel(
            guild, int(values.get("destination_channel_id") or 0)
        )
        if channel is None or guild.me is None:
            raise ValueError("Select an existing server text channel accessible to the bot.")
        permissions = channel.permissions_for(guild.me)
        if not (
            permissions.view_channel
            and permissions.send_messages
            and permissions.embed_links
            and permissions.read_message_history
        ):
            raise ValueError(
                "I need View Channel, Send Messages, Embed Links, and Read Message History "
                "in the objectives panel channel."
            )

    def save(self, guild_id: int, key: str, values: dict) -> None:
        entry = objectives._active_objectives_entry(guild_id)
        if entry is None or key != "current":
            raise ValueError("The objectives configuration was removed.")
        # Reload and update presentation only: scheduler deltas/subscribers are not a draft.
        entry.update(
            panel_title=str(values["panel_title"]).strip(),
            panel_message=str(values.get("panel_message") or "").strip(),
            destination_channel_id=int(values["destination_channel_id"]),
        )
        objectives._save_guild_entry(guild_id, entry)

    async def refresh(self, guild: discord.Guild, key: str) -> list[str]:
        async with objectives._objective_guild_locks.hold(guild.id):
            return await self._refresh_locked(guild, key)

    async def _refresh_locked(self, guild: discord.Guild, key: str) -> list[str]:
        values = self.load(guild.id, key)
        if values is None:
            return []
        try:
            pending = runtime_state.get_record(
                objectives._PANEL_PUBLICATION_RUNTIME_KIND, guild.id, "panel"
            )
            if pending is not None and pending.status in {
                "pending",
                "message_ready",
                "old_cleanup_pending",
            }:
                completed_id = await objectives._complete_panel_publication(guild, pending)
                current_record = runtime_state.get_record(
                    objectives._PANEL_PUBLICATION_RUNTIME_KIND, guild.id, "panel"
                )
                if not completed_id or (
                    current_record is not None and current_record.status != "completed"
                ):
                    return ["The previous objectives-panel move is still being completed."]
            target_channel_id = int(values["destination_channel_id"])
            channel = await objectives._resolve_text_channel(guild, target_channel_id)
            if channel is None:
                return ["The objectives panel channel is unavailable."]
            previous_channel_id, previous_message_id = objectives.get_objectives_panel_message(
                guild.id
            )
            if previous_channel_id == target_channel_id and previous_message_id:
                try:
                    message = await channel.fetch_message(previous_message_id)
                    await message.edit(
                        embed=objectives._build_panel_embed(guild),
                        view=objectives.ObjectivesPanelView(),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return []
                except discord.NotFound:
                    return RefreshWarnings(
                        [
                            "The objectives panel message was deleted. Run /set-objective-panel "
                            "if you want to replace it."
                        ],
                        retryable=False,
                    )
            record = objectives._persist_panel_publication(
                guild.id,
                target_channel_id,
                status="pending",
                previous_channel_id=previous_channel_id,
                previous_message_id=previous_message_id,
            )
            if not await objectives._complete_panel_publication(
                guild, record, discover_existing=False
            ):
                return ["The objectives-panel move was saved and will be retried."]
            final_record = runtime_state.get_record(
                objectives._PANEL_PUBLICATION_RUNTIME_KIND, guild.id, "panel"
            )
            if final_record is not None and final_record.status != "completed":
                return ["The new objectives panel is live; its old panel still needs cleanup."]
            return []
        except discord.HTTPException as error:
            return [f"Objectives panel refresh is pending: {error}"]


ADAPTER = ObjectiveConfigurationAdapter()
