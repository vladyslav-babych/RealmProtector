"""Trial configuration editing and safe refresh of already-published trial panels.

The shared editor owns authorization, concurrency control, and durable refresh
retries. This adapter never creates a channel, changes member roles, or republishes
a missing message. Lifecycle/access settings remain snapshots on existing trials;
only the user-facing title and message are propagated to active trials.
"""

from __future__ import annotations

import logging

import discord

from src.realm_protector.bot import trials
from src.realm_protector.bot.config_editor import ConfigActionsView, ConfigField, RefreshWarnings
from src.realm_protector.infrastructure import runtime_state, sqlite_database, trial_store

LOGGER = logging.getLogger(__name__)
EDITABLE_TRIAL_STATUSES = ("active", "creating")


def _configuration(guild_id: int, key: str = "main"):
    record = trial_store.config(guild_id) if key == "main" else None
    if record is None or record.status not in trials.CONFIGURATION_STATUSES:
        return None
    return record


class TrialConfigAdapter:
    kind = "trial"
    title = "Trial Configuration"
    fields: tuple[ConfigField, ...] = (
        ConfigField("category_id", "Trial category", kind="category"),
        ConfigField("role_id", "Trial role", kind="role"),
        ConfigField("manager_role_ids", "Trial Manager roles", kind="roles"),
        ConfigField("archive_channel_id", "Trial archive channel", kind="channel"),
        ConfigField("title", "Trial panel title", max_length=256),
        ConfigField("message", "Trial panel message", max_length=4000),
    )

    def load(self, guild_id: int, key: str) -> dict | None:
        record = _configuration(guild_id, key)
        return dict(record.payload) if record is not None else None

    def keys(self, guild_id: int) -> list[str]:
        return ["main"] if _configuration(guild_id) is not None else []

    def resolve_key(self, guild_id: int, message_id: int) -> str | None:
        record = _configuration(guild_id)
        if record is not None and record.payload.get("message_id") == message_id:
            return "main"
        return None

    async def validate(self, guild, key: str, values: dict) -> None:
        if self.load(guild.id, key) is None:
            raise ValueError("This trial configuration no longer exists. Run /trial-setup first.")
        for field in self.fields:
            value = values.get(field.key)
            if field.kind == "text" and (
                not isinstance(value, str) or not value.strip() or len(value) > field.max_length
            ):
                raise ValueError(f"{field.label} must contain 1-{field.max_length} characters.")
        trials.validate_configuration(guild, values)

    def save(self, guild_id: int, key: str, values: dict) -> None:
        configuration = _configuration(guild_id, key)
        if configuration is None:
            raise ValueError("This trial configuration no longer exists. Run /trial-setup first.")
        payload = {
            **configuration.payload,
            **{field.key: values[field.key] for field in self.fields},
        }
        records = runtime_state.list_records(
            trial_store.TRIAL, guild_id=guild_id, statuses=EDITABLE_TRIAL_STATUSES
        )
        # Commit every rendering snapshot with the configuration. A failed HTTP
        # edit cannot cause restart recovery to restore the previous panel text.
        with sqlite_database.transaction() as database:
            runtime_state.upsert_record_in_transaction(
                database,
                configuration.kind,
                guild_id,
                configuration.external_id,
                payload,
                status=configuration.status,
            )
            for record in records:
                updated = {
                    **record.payload,
                    "config": {
                        **record.payload["config"],
                        "title": payload["title"],
                        "message": payload["message"],
                    },
                }
                runtime_state.upsert_record_in_transaction(
                    database,
                    record.kind,
                    guild_id,
                    record.external_id,
                    updated,
                    status=record.status,
                )

    async def refresh(self, guild, key: str) -> list[str]:
        configuration = _configuration(guild.id, key)
        if configuration is None:
            return []
        warnings = RefreshWarnings(retryable=False)
        warning, retryable = await _edit_existing_message(
            guild,
            configuration.payload.get("panel_channel_id"),
            configuration.payload.get("message_id"),
            embed=trials.configuration_embed(configuration.payload),
            view=ConfigActionsView(self.kind, key),
            label="Trial configuration panel",
        )
        if warning:
            warnings.append(warning)
            warnings.retryable |= retryable
        for record in runtime_state.list_records(
            trial_store.TRIAL, guild_id=guild.id, statuses=EDITABLE_TRIAL_STATUSES
        ):
            if record.status == "creating" and not record.payload.get("message_id"):
                # Trial lifecycle recovery will publish from the updated snapshot.
                continue
            warning, retryable = await _edit_existing_message(
                guild,
                record.payload.get("channel_id"),
                record.payload.get("message_id"),
                embed=trials.trial_embed(record.payload),
                view=trials.TrialView(),
                label=f"Trial panel for <@{record.payload['member_id']}>",
            )
            if warning:
                warnings.append(warning)
                warnings.retryable |= retryable
        return warnings


def _normalized_components(components: list[dict]) -> list[dict]:
    """Ignore Discord-generated component IDs, retaining meaningful button state."""
    result = []
    for component in components:
        normalized = {key: value for key, value in component.items() if key != "id"}
        if "components" in normalized:
            normalized["components"] = _normalized_components(normalized["components"])
        result.append(normalized)
    return result


def _message_matches(message, embed: discord.Embed, view: discord.ui.View) -> bool:
    existing_embeds = getattr(message, "embeds", None)
    existing_components = getattr(message, "components", None)
    if existing_embeds is None or existing_components is None:
        return False
    return [value.to_dict() for value in existing_embeds] == [embed.to_dict()] and (
        _normalized_components([value.to_dict() for value in existing_components])
        == _normalized_components(view.to_components())
    )


async def _edit_existing_message(
    guild, channel_id, message_id, *, embed, view, label
) -> tuple[str | None, bool]:
    if not channel_id or not message_id:
        return f"{label} has no recorded message location; no replacement was posted.", False
    try:
        channel = await guild.fetch_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return (
                f"{label} is not in an available text channel; no replacement was posted.",
                False,
            )
        message = await channel.fetch_message(message_id)
        if not _message_matches(message, embed, view):
            await message.edit(embed=embed, view=view, allowed_mentions=trials.NO_MENTIONS)
    except discord.NotFound:
        return f"{label} or its channel was deleted; no replacement was posted.", False
    except discord.HTTPException:
        LOGGER.exception("Unable to update %s in guild %s", label, guild.id)
        return (
            f"{label} could not be updated. Check the bot's channel permissions; refresh will retry.",
            True,
        )
    return None, False


ADAPTER = TrialConfigAdapter()
