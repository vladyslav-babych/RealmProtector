"""Shared, private configuration editing and durable public-panel refreshes.

Feature adapters own validation, persistence and Discord projections. Drafts and
refresh intents live in SQLite; an editor never treats its preview as live state.
"""

from __future__ import annotations

import copy
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

import discord

from src.realm_protector.infrastructure import guild_settings, runtime_state
from src.realm_protector.services import authorization, guild_lifecycle

LOGGER = logging.getLogger(__name__)
DRAFT_KIND = "configuration_edit_draft"
REFRESH_KIND = "configuration_edit_refresh"
EDITOR_LIFETIME = 900
NO_MENTIONS = discord.AllowedMentions.none()


@dataclass(frozen=True)
class ConfigField:
    key: str
    label: str
    kind: str = "text"
    max_length: int = 4000
    options: tuple[tuple[str, str], ...] = ()
    required: bool = True


class RefreshWarnings(list[str]):
    """Notices can require manual repair without causing an endless HTTP retry."""

    def __init__(self, messages=(), *, retryable: bool = True):
        super().__init__(messages)
        self.retryable = retryable


class ConfigAdapter(Protocol):
    kind: str
    title: str
    fields: tuple[ConfigField, ...]

    def load(self, guild_id: int, key: str) -> dict | None: ...

    async def validate(self, guild, key: str, values: dict) -> None: ...

    def save(self, guild_id: int, key: str, values: dict) -> None: ...

    async def refresh(self, guild, key: str) -> list[str]: ...

    def keys(self, guild_id: int) -> list[str]: ...

    def resolve_key(self, guild_id: int, message_id: int) -> str | None: ...


def get_adapter(kind: str) -> ConfigAdapter:
    # Lazy imports avoid coupling existing setup/publication modules to one another.
    from src.realm_protector.bot import (
        bot_configuration,
        objective_configuration,
        reaction_configuration,
        ticket_configuration,
        trial_configuration,
    )

    adapters: dict[str, ConfigAdapter] = {
        "bot": bot_configuration.ADAPTER,
        "ticket": ticket_configuration.ADAPTER,
        "trial": trial_configuration.ADAPTER,
        "reaction": reaction_configuration.ADAPTER,
        "objective": objective_configuration.ADAPTER,
    }
    if kind not in adapters:
        raise ValueError("This configuration type is no longer supported.")
    return adapters[kind]


def _field(adapter: ConfigAdapter, key: str | None) -> ConfigField | None:
    return next((field for field in adapter.fields if field.key == key), None)


async def _notice(interaction, text: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS)
    else:
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS)


def _require_admin(interaction) -> None:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        raise ValueError("Configuration can only be updated inside its Discord server.")
    if not authorization.member_is_admin(interaction.user):
        raise ValueError("Only server administrators can update configuration.")


def _save_draft(record, **updates):
    return runtime_state.upsert_record(
        DRAFT_KIND,
        record.guild_id,
        record.external_id,
        {**record.payload, **updates},
        status=record.status,
    )


def read_draft(interaction, draft_id: str, *, allow_modal: bool = False):
    _require_admin(interaction)
    record = runtime_state.get_record(DRAFT_KIND, interaction.guild.id, draft_id)
    if record is None or record.status != "active":
        raise ValueError("This editor has ended. Click Update Config to open a new one.")
    if record.payload["user_id"] != interaction.user.id:
        raise ValueError("Only the administrator who opened this private editor can use it.")
    if record.payload["expires_at"] <= time.time():
        runtime_state.set_status(DRAFT_KIND, record.guild_id, record.external_id, "expired")
        raise ValueError("This editor has expired. Click Update Config to open a new one.")
    message_id = getattr(interaction.message, "id", None)
    if message_id != record.payload.get("message_id") and not (allow_modal and message_id is None):
        raise ValueError("This is an outdated editor. Use your latest private configuration panel.")
    if guild_settings.get_target_guild(record.guild_id) != record.payload.get("target_guild"):
        raise ValueError("The server setup changed. Open a new configuration editor.")
    if get_adapter(record.payload["kind"]).load(record.guild_id, record.payload["key"]) is None:
        raise ValueError("This configuration was removed or disabled. No changes were made.")
    return record


def _preview(field: ConfigField, value) -> str:
    if value is None or value == "" or value == []:
        return "Not configured"
    if field.kind in {"role", "roles"}:
        ids = value if isinstance(value, list) else [value]
        return ", ".join(f"<@&{role_id}>" for role_id in ids)
    if field.kind in {"channel", "category"}:
        return f"<#{value}>"
    if field.kind == "choice":
        return next((label for label, key in field.options if key == value), str(value))
    if field.kind == "reactions":
        return "\n".join(f"{entry['emoji']} → <@&{entry['role_id']}>" for entry in value)
    return str(value)


def _preview_fields(embed: discord.Embed, name: str, value: str) -> None:
    # Discord's 6000-character limit applies to the whole message, not each embed.
    # Both full values remain in SQLite and text inputs; only long previews shorten.
    if len(value) > 2300:
        value = value[:2200] + "\n… Preview shortened; the full text is kept."
    for offset in range(0, len(value), 1024):
        embed.add_field(
            name=name if offset == 0 else f"{name} (continued)",
            value=value[offset : offset + 1024],
            inline=False,
        )


def editor_embed(record) -> discord.Embed:
    adapter = get_adapter(record.payload["kind"])
    field = _field(adapter, record.payload.get("selected"))
    embed = discord.Embed(title=f"Update {adapter.title}", color=discord.Color.blurple())
    if field is None:
        embed.description = "## :gear: Select the configuration point you want to change"
    else:
        embed.description = f"## :pencil: {field.label}\nReview the preview, then **Save Change**."
        if adapter.kind == "bot" and field.key == "guild_name":
            embed.description += (
                "\n⚠️ Changing the Albion guild archives the previous guild's local ledger "
                "and disables its Google Sheet link. This is not a cosmetic rename."
            )
        current = adapter.load(record.guild_id, record.payload["key"]) or {}
        _preview_fields(embed, "Current configuration", _preview(field, current.get(field.key)))
        _preview_fields(
            embed, "New configuration preview", _preview(field, record.payload.get("pending"))
        )
        if field.kind == "reactions" and record.payload.get("reaction_role_id"):
            embed.add_field(
                name="Role for next reaction",
                value=f"<@&{record.payload['reaction_role_id']}>",
                inline=False,
            )
    if record.payload.get("status_message"):
        embed.add_field(
            name="Status", value=str(record.payload["status_message"])[:1024], inline=False
        )
    embed.set_footer(
        text="Private editor • Changes apply only after saving • Expires after 15 minutes"
    )
    return embed


def queue_refresh(guild_id: int, kind: str, key: str):
    """Write intent before configuration; a crash before save is a harmless refresh."""
    return runtime_state.upsert_record(
        REFRESH_KIND, guild_id, f"{kind}:{key}", {"kind": kind, "key": key}, status="pending"
    )


async def refresh_configuration(guild, kind: str, key: str) -> list[str]:
    """Caller holds the guild lifecycle lock; incomplete projections stay retryable."""
    adapter = get_adapter(kind)
    record_id = f"{kind}:{key}"
    if adapter.load(guild.id, key) is None:
        runtime_state.set_status(REFRESH_KIND, guild.id, record_id, "cancelled")
        return []
    try:
        warnings = await adapter.refresh(guild, key)
    except Exception:
        LOGGER.exception("Configuration refresh failed: %s/%s/%s", guild.id, kind, key)
        warnings = ["Discord panels could not all be refreshed. Check the bot's permissions."]
    runtime_state.upsert_record(
        REFRESH_KIND,
        guild.id,
        record_id,
        {"kind": kind, "key": key, "warnings": warnings},
        status="pending" if warnings and getattr(warnings, "retryable", True) else "completed",
    )
    return warnings


async def open_editor(interaction, kind: str, key: str | None = "main") -> None:
    try:
        _require_admin(interaction)
        adapter = get_adapter(kind)
        if not guild_settings.get_target_guild(interaction.guild.id):
            raise ValueError(
                "This server setup was removed or is not configured. Run /bot-setup first."
            )
        if key == "current":
            key = adapter.resolve_key(interaction.guild.id, getattr(interaction.message, "id", 0))
        if not key or adapter.load(interaction.guild.id, key) is None:
            raise ValueError("This configuration no longer exists or this panel has been replaced.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        record = runtime_state.upsert_record(
            DRAFT_KIND,
            interaction.guild.id,
            uuid4().hex,
            {
                "kind": kind,
                "key": key,
                "user_id": interaction.user.id,
                "expires_at": time.time() + EDITOR_LIFETIME,
                "revision": 0,
                "target_guild": guild_settings.get_target_guild(interaction.guild.id),
            },
        )
        message = await interaction.edit_original_response(
            embed=editor_embed(record),
            view=ConfigEditorView(record, interaction.guild),
            allowed_mentions=NO_MENTIONS,
        )
        _save_draft(record, message_id=message.id)
    except ValueError as error:
        await _notice(interaction, str(error))


class UpdateConfigButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"realm:config:update:(?P<kind>[a-z]+):(?P<key>[A-Za-z0-9_-]+)",
):
    def __init__(self, kind: str, key: str = "main"):
        self.kind = kind
        self.key = key
        super().__init__(
            discord.ui.Button(
                label="Update Config",
                style=discord.ButtonStyle.secondary,
                custom_id=f"realm:config:update:{kind}:{key}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["kind"], match["key"])

    async def callback(self, interaction):
        await open_editor(interaction, self.kind, self.key)


class ConfigActionsView(discord.ui.View):
    def __init__(self, kind: str, key: str = "main"):
        super().__init__(timeout=None)
        self.add_item(UpdateConfigButton(kind, key))


class ConfigEditorView(discord.ui.View):
    def __init__(self, record, guild=None):
        super().__init__(timeout=None)
        self.record = record
        self.draft_id = record.external_id
        self.revision = record.payload.get("revision", 0)
        self.guild = guild
        adapter = get_adapter(record.payload["kind"])
        selected = record.payload.get("selected")
        self.add_item(_FieldSelect(self, adapter.fields, selected))
        field = _field(adapter, selected)
        if field is not None:
            pending = record.payload.get("pending")
            if field.kind in {"role", "roles", "reactions"}:
                self.add_item(_RoleSelect(self, field, pending))
            if field.kind in {"channel", "category"}:
                self.add_item(_ChannelSelect(self, field, pending))
            if field.kind == "choice":
                self.add_item(_ChoiceSelect(self, field, pending))
            if field.kind == "text":
                self.add_item(_ActionButton(self, "edit", "Set Value", discord.ButtonStyle.primary))
            if field.kind == "reactions":
                self.add_item(
                    _ActionButton(
                        self, "emoji", "Add / Replace Emoji", discord.ButtonStyle.primary, row=3
                    )
                )
                if pending:
                    self.add_item(_RemoveReactionSelect(self, pending))
            self.add_item(
                _ActionButton(self, "save", "Save Change", discord.ButtonStyle.success, row=4)
            )
        self.add_item(
            _ActionButton(self, "cancel", "Done / Cancel", discord.ButtonStyle.secondary, row=4)
        )

    def custom_id(self, action):
        return f"realm:config:d:{self.draft_id}:{self.revision}:{action}"

    async def interaction_check(self, interaction):
        try:
            record = read_draft(interaction, self.draft_id)
            if record.payload.get("revision", 0) != self.revision:
                raise ValueError("This preview changed. Use the current editor controls.")
        except ValueError as error:
            await _notice(interaction, str(error))
            return False
        return True

    async def render(self, interaction, record):
        await interaction.edit_original_response(
            embed=editor_embed(record),
            view=ConfigEditorView(record, interaction.guild),
            allowed_mentions=NO_MENTIONS,
        )
        self.stop()

    async def act(self, interaction, action: str, values=None):
        try:
            record = read_draft(interaction, self.draft_id)
            if record.payload.get("revision", 0) != self.revision:
                raise ValueError("This preview changed. Use the current editor controls.")
            if action in {"edit", "emoji"}:
                if action == "emoji" and not record.payload.get("reaction_role_id"):
                    raise ValueError("Select the role for this reaction first.")
                record = _save_draft(
                    record,
                    modal_revision=self.revision,
                    modal_field=record.payload.get("selected"),
                    modal_emoji=action == "emoji",
                )
                await interaction.response.send_modal(
                    ConfigValueModal(record, emoji=action == "emoji")
                )
                return
            await interaction.response.defer()
            async with guild_lifecycle.lock_for(record.guild_id):
                record = read_draft(interaction, self.draft_id)
                if record.payload.get("revision", 0) != self.revision:
                    raise ValueError("This preview changed. Use the current editor controls.")
                adapter = get_adapter(record.payload["kind"])
                selected = _field(adapter, record.payload.get("selected"))
                revision = self.revision + 1
                if action == "cancel":
                    runtime_state.set_status(DRAFT_KIND, record.guild_id, self.draft_id, "closed")
                    await interaction.edit_original_response(
                        embed=discord.Embed(
                            title="Configuration editor closed",
                            description="Unsaved previews were discarded. Previously saved changes are kept.",
                        ),
                        view=None,
                    )
                    self.stop()
                    return
                if action == "field":
                    field = _field(adapter, values[0])
                    if field is None:
                        raise ValueError("Select a valid configuration point.")
                    current = adapter.load(record.guild_id, record.payload["key"]) or {}
                    original = copy.deepcopy(current.get(field.key))
                    record = _save_draft(
                        record,
                        selected=field.key,
                        original=original,
                        pending=copy.deepcopy(original),
                        revision=revision,
                        reaction_role_id=None,
                        status_message=None,
                    )
                elif selected is None:
                    raise ValueError("Select a configuration point first.")
                elif action == "value":
                    if selected.kind == "reactions":
                        record = _save_draft(
                            record, reaction_role_id=int(values[0]), revision=revision
                        )
                    else:
                        value = (
                            values if selected.kind == "roles" else (values[0] if values else None)
                        )
                        record = _save_draft(
                            record, pending=value, revision=revision, status_message=None
                        )
                elif action == "remove_reaction":
                    pending = copy.deepcopy(record.payload.get("pending") or [])
                    index = int(values[0])
                    if not 0 <= index < len(pending):
                        raise ValueError("This reaction list changed. Select it again.")
                    pending.pop(index)
                    record = _save_draft(
                        record, pending=pending, revision=revision, status_message=None
                    )
                elif action == "save":
                    record = await save_change(interaction, record, selected, revision)
                await self.render(interaction, record)
        except ValueError as error:
            await _notice(interaction, str(error))
        except Exception:
            LOGGER.exception("Private configuration editor action failed")
            await _notice(
                interaction,
                "The operation could not finish. Reopen the editor to check the saved configuration; pending panel refreshes will retry automatically.",
            )


def _validate_value(field: ConfigField, value) -> None:
    if field.required and (value is None or value == "" or value == []):
        raise ValueError(f"Set {field.label.lower()} before saving.")
    if field.kind == "text" and (not isinstance(value, str) or len(value) > field.max_length):
        raise ValueError(f"{field.label} must contain at most {field.max_length} characters.")
    if field.kind == "choice" and value not in {key for _, key in field.options}:
        raise ValueError("Select one of the available options.")


async def save_change(interaction, record, field: ConfigField, revision: int):
    """Patch just one field, rejecting conflicting edits rather than losing changes."""
    _require_admin(interaction)
    adapter = get_adapter(record.payload["kind"])
    key = record.payload["key"]
    current = adapter.load(record.guild_id, key)
    if current is None:
        raise ValueError("This configuration was removed. No changes were made.")
    if current.get(field.key) != record.payload.get("original"):
        raise ValueError(
            "Another administrator changed this configuration point. Select it again to reload the current value before saving."
        )
    value = record.payload.get("pending")
    _validate_value(field, value)
    if value == current.get(field.key):
        return _save_draft(record, revision=revision, status_message="No changes to save.")
    updated = {**copy.deepcopy(current), field.key: copy.deepcopy(value)}
    await adapter.validate(interaction.guild, key, updated)
    _require_admin(interaction)
    latest = adapter.load(record.guild_id, key)
    if latest != current or guild_settings.get_target_guild(record.guild_id) != record.payload.get(
        "target_guild"
    ):
        raise ValueError(
            "Configuration changed during validation. Select this point again before saving."
        )
    # An intent exists before every local write. If save fails it can safely
    # project the unchanged source; no Discord write happens before save succeeds.
    queue_refresh(record.guild_id, adapter.kind, key)
    adapter.save(record.guild_id, key, updated)
    current = adapter.load(record.guild_id, key) or updated
    warnings = await refresh_configuration(interaction.guild, adapter.kind, key)
    status = "Configuration saved. Associated live panels updated."
    if warnings:
        status = "Configuration saved in SQLite. "
        status += (
            "Some panels could not refresh; they will retry automatically.\n"
            if getattr(warnings, "retryable", True)
            else "Some deleted or missing panels need administrator attention.\n"
        )
        status += "\n".join(warnings)
    if adapter.kind == "bot" and field.key == "guild_name":
        status += "\nChanging the Albion guild archives its previous ledger and quarantines any previous Sheet link; link Google Sheets again if needed."
    return _save_draft(
        record,
        original=copy.deepcopy(current.get(field.key)),
        pending=copy.deepcopy(current.get(field.key)),
        revision=revision,
        target_guild=guild_settings.get_target_guild(record.guild_id),
        status_message=status,
    )


class _FieldSelect(discord.ui.Select):
    def __init__(self, editor, fields, selected):
        super().__init__(
            placeholder="Which configuration point do you want to change?",
            row=0,
            custom_id=editor.custom_id("field"),
            options=[
                discord.SelectOption(
                    label=field.label, value=field.key, default=field.key == selected
                )
                for field in fields
            ],
        )

    async def callback(self, interaction):
        if isinstance(self.view, ConfigEditorView):
            await self.view.act(interaction, "field", self.values)


class _RoleSelect(discord.ui.RoleSelect):
    def __init__(self, editor, field, pending):
        ids = pending if isinstance(pending, list) else ([pending] if pending else [])
        if field.kind == "reactions":
            ids = (
                [editor.record.payload["reaction_role_id"]]
                if editor.record.payload.get("reaction_role_id")
                else []
            )
        if editor.guild is not None:
            ids = [role_id for role_id in ids if editor.guild.get_role(int(role_id)) is not None]
        super().__init__(
            placeholder="Select role(s)",
            row=1,
            custom_id=editor.custom_id("value"),
            min_values=1 if field.required else 0,
            max_values=25 if field.kind == "roles" else 1,
            default_values=[discord.Object(id=int(role_id)) for role_id in ids],
        )

    async def callback(self, interaction):
        if isinstance(self.view, ConfigEditorView):
            await self.view.act(interaction, "value", [role.id for role in self.values])


class _ChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, editor, field, pending):
        if pending and editor.guild is not None and editor.guild.get_channel(int(pending)) is None:
            pending = None
        super().__init__(
            placeholder=f"Select {field.label.lower()}",
            row=1,
            custom_id=editor.custom_id("value"),
            min_values=1 if field.required else 0,
            channel_types=[
                discord.ChannelType.category
                if field.kind == "category"
                else discord.ChannelType.text
            ],
            default_values=[discord.Object(id=int(pending))] if pending else [],
        )

    async def callback(self, interaction):
        if isinstance(self.view, ConfigEditorView):
            await self.view.act(interaction, "value", [channel.id for channel in self.values])


class _ChoiceSelect(discord.ui.Select):
    def __init__(self, editor, field, pending):
        super().__init__(
            placeholder=f"Select {field.label.lower()}",
            row=1,
            custom_id=editor.custom_id("value"),
            options=[
                discord.SelectOption(label=label, value=key, default=key == pending)
                for label, key in field.options
            ],
        )

    async def callback(self, interaction):
        if isinstance(self.view, ConfigEditorView):
            await self.view.act(interaction, "value", self.values)


class _RemoveReactionSelect(discord.ui.Select):
    def __init__(self, editor, pending):
        super().__init__(
            placeholder="Remove a reaction from the preview",
            row=2,
            custom_id=editor.custom_id("remove_reaction"),
            options=[
                discord.SelectOption(
                    label=f"{entry['emoji']} → role {entry['role_id']}"[:100], value=str(index)
                )
                for index, entry in enumerate(pending[:25])
            ],
        )

    async def callback(self, interaction):
        if isinstance(self.view, ConfigEditorView):
            await self.view.act(interaction, "remove_reaction", self.values)


class _ActionButton(discord.ui.Button):
    def __init__(self, editor, action, label, style, *, row=2):
        self.action = action
        super().__init__(label=label, style=style, row=row, custom_id=editor.custom_id(action))

    async def callback(self, interaction):
        if isinstance(self.view, ConfigEditorView):
            await self.view.act(interaction, self.action)


class ConfigValueModal(discord.ui.Modal):
    def __init__(self, record, *, emoji=False):
        adapter = get_adapter(record.payload["kind"])
        field = _field(adapter, record.payload.get("selected"))
        assert field is not None
        super().__init__(
            title=("Set Reaction Emoji" if emoji else f"Set {field.label}")[:45],
            timeout=EDITOR_LIFETIME,
            custom_id=f"realm:config:modal:{record.external_id}:{record.payload.get('revision', 0)}:{field.key}",
        )
        self.draft_id = record.external_id
        self.revision = record.payload.get("revision", 0)
        self.field_key = field.key
        self.is_emoji = emoji
        self.value_input: discord.ui.TextInput[ConfigValueModal] = discord.ui.TextInput(
            label="Emoji" if emoji else field.label[:45],
            default="" if emoji else str(record.payload.get("pending") or ""),
            required=field.required,
            max_length=100 if emoji else field.max_length,
            style=discord.TextStyle.short
            if emoji or field.max_length <= 256
            else discord.TextStyle.paragraph,
            custom_id="configuration_value",
        )
        self.add_item(self.value_input)

    async def on_submit(self, interaction):
        try:
            _require_admin(interaction)
            await interaction.response.defer()
            async with guild_lifecycle.lock_for(interaction.guild.id):
                record = read_draft(interaction, self.draft_id, allow_modal=True)
                if (
                    record.payload.get("selected") != self.field_key
                    or record.payload.get("revision", 0) != self.revision
                ):
                    raise ValueError(
                        "The selected configuration point changed. Open its text editor again."
                    )
                value: Any = str(self.value_input).strip()
                if self.is_emoji:
                    from src.realm_protector.bot import reaction_roles

                    emoji = reaction_roles._normalize_emoji_input(value)
                    if not emoji:
                        raise ValueError("Enter one valid emoji.")
                    value = copy.deepcopy(record.payload.get("pending") or [])
                    key = reaction_roles._emoji_key(emoji)
                    value = [
                        entry for entry in value if reaction_roles._emoji_key(entry["emoji"]) != key
                    ]
                    if len(value) >= reaction_roles._MAX_REACTIONS_PER_PANEL:
                        raise ValueError(
                            f"A panel supports at most {reaction_roles._MAX_REACTIONS_PER_PANEL} reaction mappings."
                        )
                    value.append({"emoji": emoji, "role_id": record.payload["reaction_role_id"]})
                record = _save_draft(
                    record,
                    pending=value,
                    revision=self.revision + 1,
                    status_message=None,
                    modal_revision=None,
                    modal_field=None,
                )
                await interaction.edit_original_response(
                    embed=editor_embed(record),
                    view=ConfigEditorView(record, interaction.guild),
                    allowed_mentions=NO_MENTIONS,
                )
        except ValueError as error:
            await _notice(interaction, str(error))


def register_persistent_views(bot) -> None:
    bot.add_dynamic_items(UpdateConfigButton)
    for record in runtime_state.list_records(DRAFT_KIND, statuses=("active",)):
        if record.payload.get("expires_at", 0) <= time.time():
            runtime_state.set_status(DRAFT_KIND, record.guild_id, record.external_id, "expired")
            continue
        if record.payload.get("message_id"):
            try:
                bot.add_view(ConfigEditorView(record), message_id=record.payload["message_id"])
                if record.payload.get("modal_revision") is not None:
                    # discord.py stores modals in its view store, not add_view's
                    # View-only public API. Rebuild the exact custom IDs so an
                    # already-open Discord form can still be submitted after restart.
                    modal_record = runtime_state.RuntimeRecord(
                        kind=record.kind,
                        guild_id=record.guild_id,
                        external_id=record.external_id,
                        payload={
                            **record.payload,
                            "selected": record.payload["modal_field"],
                            "revision": record.payload["modal_revision"],
                        },
                        status=record.status,
                        updated_at=record.updated_at,
                    )
                    bot._connection.store_view(
                        ConfigValueModal(
                            modal_record, emoji=record.payload.get("modal_emoji", False)
                        )
                    )
            except (ValueError, KeyError, TypeError):
                LOGGER.exception(
                    "Could not restore private configuration editor %s", record.external_id
                )


async def reconcile_configuration_updates(bot) -> None:
    """Backfill buttons on existing saved panels once, then retry unfinished updates."""
    for draft in runtime_state.list_records(DRAFT_KIND, statuses=("active",)):
        if draft.payload.get("expires_at", 0) <= time.time():
            runtime_state.set_status(DRAFT_KIND, draft.guild_id, draft.external_id, "expired")
    # Persistent routing is needed for restart recovery, but expired private
    # editors must not accumulate forever in a long-running bot's view store.
    for view in tuple(getattr(bot, "persistent_views", ())):
        if isinstance(view, ConfigEditorView):
            current_draft = runtime_state.get_record(
                DRAFT_KIND, view.record.guild_id, view.draft_id
            )
            if current_draft is None or current_draft.status != "active":
                view.stop()
    for guild in getattr(bot, "guilds", ()):
        async with guild_lifecycle.lock_for(guild.id):
            for pending in runtime_state.list_records(
                REFRESH_KIND, guild_id=guild.id, statuses=("pending",)
            ):
                if (
                    not guild_settings.get_target_guild(guild.id)
                    or get_adapter(pending.payload["kind"]).load(guild.id, pending.payload["key"])
                    is None
                ):
                    runtime_state.set_status(
                        REFRESH_KIND, guild.id, pending.external_id, "cancelled"
                    )
            if not guild_settings.get_target_guild(guild.id):
                continue
            for kind in ("bot", "ticket", "trial", "reaction", "objective"):
                adapter = get_adapter(kind)
                for key in adapter.keys(guild.id):
                    record = runtime_state.get_record(REFRESH_KIND, guild.id, f"{kind}:{key}")
                    if record is None:
                        queue_refresh(guild.id, kind, key)
                    if record is None or record.status == "pending":
                        await refresh_configuration(guild, kind, key)
