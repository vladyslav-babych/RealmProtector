import hashlib
import logging
from typing import Optional
from uuid import uuid4

import discord
import emoji

from src.realm_protector.bot import message_checkpoints
from src.realm_protector.infrastructure import (
    document_store,
    guild_settings,
    runtime_state,
)
from src.realm_protector.services import authorization, role_security
from src.realm_protector.services.keyed_locks import KeyedLockPool

_MAX_REACTIONS_PER_PANEL = 6
_PUBLISH_RUNTIME_KIND = "reaction_panel_publish"
_PUBLISH_MARKER_PREFIX = "realm-protector:reaction-panel-publish:"
_PANEL_PAGE_SIZE = 25
_MAX_OFFLINE_RECONCILIATION_USERS = 500
_REACTION_ROLE_CONFIG_NAMESPACE = "reaction_roles"
_FALLBACK_HISTORY_LIMIT = None
_member_role_locks: KeyedLockPool[tuple[int, int, int]] = KeyedLockPool()
_offline_reconciled_panel_versions: dict[tuple[int, int], str] = {}


class _PanelPublishError(RuntimeError):
    """Raised after a reaction-panel publication was safely compensated."""


async def _send_ephemeral_notice(interaction: discord.Interaction, text: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


async def _ensure_current_admin_owner(
    interaction: discord.Interaction,
    user_id: int,
    guild: discord.Guild,
) -> bool:
    if interaction.user.id != user_id:
        await _send_ephemeral_notice(
            interaction,
            "Only the admin who opened this setup can use these controls.",
        )
        return False
    if (
        interaction.guild is None
        or interaction.guild.id != guild.id
        or not isinstance(interaction.user, discord.Member)
        or not await authorization.is_admin(interaction.user)
    ):
        await _send_ephemeral_notice(
            interaction,
            "Your Administrator permission was removed; these controls can no longer be used.",
        )
        return False
    return True


def _strip_variation_selectors(value: str) -> str:
    return (value or "").replace("\ufe0f", "")


def _extract_single_unicode_emoji(value: str) -> Optional[str]:
    text = (value or "").strip()
    if not text:
        return None

    found = emoji.emoji_list(text)
    if len(found) != 1:
        return None

    emoji_char = found[0].get("emoji")
    if not isinstance(emoji_char, str) or not emoji_char:
        return None

    remainder = text.replace(emoji_char, "", 1).strip()
    if remainder:
        return None

    return emoji_char


def _normalize_emoji_input(raw_value: str) -> Optional[str]:
    value = (raw_value or "").strip()
    if not value:
        return None

    if any(not c.isascii() for c in value):
        return _extract_single_unicode_emoji(value)

    if value.startswith(":") and value.endswith(":") and len(value) > 2:
        converted = emoji.emojize(value, language="alias")
        if converted != value:
            return _extract_single_unicode_emoji(converted)

        converted = emoji.emojize(value)
        if converted != value:
            return _extract_single_unicode_emoji(converted)

    return None


def _emoji_key(emoji_raw: str) -> str:
    return _strip_variation_selectors((emoji_raw or "").strip())


def _emoji_matches(left: str, right: str) -> bool:
    return _emoji_key(left) == _emoji_key(right)


async def _add_panel_reaction(message: discord.Message, raw_emoji: str) -> bool:
    normalized = _normalize_emoji_input(raw_emoji)
    if not normalized:
        logging.warning("Skipping invalid reaction emoji %s for message %s", raw_emoji, message.id)
        return False

    try:
        await message.add_reaction(normalized)
    except discord.HTTPException as err:
        logging.warning("Could not add reaction %s: %s", normalized, err)
        return False
    return True


def _load_guild_entry(guild_id: int) -> Optional[dict]:
    entry = document_store.get_mapping_entry(
        _REACTION_ROLE_CONFIG_NAMESPACE,
        guild_id,
    )
    return entry if isinstance(entry, dict) else None


def _save_guild_entry(guild_id: int, entry: dict) -> None:
    document_store.upsert_mapping_entry(
        _REACTION_ROLE_CONFIG_NAMESPACE,
        guild_id,
        entry,
    )


async def deactivate_guild_reaction_role_configuration(
    guild: discord.Guild,
) -> bool:
    """Make stored panels inert, then remove their reaction-routing metadata."""

    entry = _load_guild_entry(guild.id)
    if not isinstance(entry, dict):
        return False
    entry["disabled"] = True
    _save_guild_entry(guild.id, entry)
    panels = entry.get("panels", {})
    all_clean = True

    if isinstance(panels, dict):
        for panel in panels.values():
            if not isinstance(panel, dict):
                continue
            try:
                channel_id = int(panel.get("panel_channel_id") or 0)
                message_id = int(panel.get("panel_message_id") or 0)
            except (TypeError, ValueError):
                continue
            try:
                channel = guild.get_channel(channel_id) or await guild.fetch_channel(channel_id)
                if isinstance(channel, discord.TextChannel):
                    message = await channel.fetch_message(message_id)
                    await message.clear_reactions()
                    await message.edit(
                        content="This reaction-role panel has been disabled.",
                        embed=None,
                    )
            except discord.NotFound:
                continue
            except (discord.Forbidden, discord.HTTPException):
                all_clean = False

    if all_clean:
        document_store.delete_mapping_entry(
            _REACTION_ROLE_CONFIG_NAMESPACE,
            guild.id,
        )
    return all_clean


def _guild_entry(entry: Optional[dict]) -> dict:
    if not isinstance(entry, dict):
        entry = {"panels": {}}
    entry.setdefault("panels", {})
    return entry


def _list_panels(guild_id: int) -> list[dict]:
    entry = _load_guild_entry(guild_id)
    if not isinstance(entry, dict) or entry.get("disabled"):
        return []
    panels = entry.get("panels", {})
    return list(panels.values()) if isinstance(panels, dict) else []


def _get_panel_by_message_id(guild_id: int, message_id: int) -> Optional[dict]:
    for panel in _list_panels(guild_id):
        if int(panel.get("panel_message_id", 0) or 0) == int(message_id):
            return panel
    return None


def _get_panel_by_id(guild_id: int, panel_id: str) -> Optional[dict]:
    entry = _load_guild_entry(guild_id)
    if not isinstance(entry, dict) or entry.get("disabled"):
        return None
    panels = entry.get("panels", {})
    if not isinstance(panels, dict):
        return None
    panel = panels.get(str(panel_id))
    return panel if isinstance(panel, dict) else None


def _save_panel(guild_id: int, panel: dict) -> None:
    entry = _guild_entry(_load_guild_entry(guild_id))
    entry["panels"][str(panel["id"])] = panel
    _save_guild_entry(guild_id, entry)


def _delete_panel(guild_id: int, panel_id: str) -> None:
    entry = _load_guild_entry(guild_id)
    if not isinstance(entry, dict):
        return
    panels = entry.get("panels", {})
    if isinstance(panels, dict):
        panels.pop(str(panel_id), None)
    _save_guild_entry(guild_id, entry)


def _format_role_mention(guild: discord.Guild, role_id: Optional[int]) -> str:
    if not role_id:
        return "Not selected"
    role = guild.get_role(int(role_id))
    return role.mention if role is not None else "Not selected"


def _format_channel_mention(guild: discord.Guild, channel_id: Optional[int]) -> str:
    if not channel_id:
        return "Not selected"
    ch = guild.get_channel(int(channel_id))
    return ch.mention if ch is not None else "Not selected"


def _format_role_reaction_list(guild: discord.Guild, reactions: list[dict]) -> str:
    if not reactions:
        return "None"
    lines: list[str] = []
    for item in reactions:
        role = guild.get_role(int(item.get("role_id", 0) or 0))
        role_mention = role.mention if role is not None else f"<unknown role {item.get('role_id')}>"
        lines.append(f"{item.get('emoji', '')} - {role_mention}")
    return "\n".join(lines)


def _build_panel_embed(
    panel_name: str,
    panel_message: str,
    guild: discord.Guild,
    reactions: list[dict],
) -> discord.Embed:
    description = panel_message
    if reactions:
        description += "\n\n" + _format_role_reaction_list(guild, reactions)
    return discord.Embed(title=panel_name, description=description)


def _publish_marker(operation_id: str) -> str:
    return f"{_PUBLISH_MARKER_PREFIX}{operation_id}"


def _publish_payload(panel: dict, *, operation: str) -> dict:
    return {
        "operation": str(operation),
        "panel_id": str(panel.get("id") or ""),
        "panel": dict(panel),
    }


def _record_publish(
    guild_id: int,
    operation_id: str,
    panel: dict,
    *,
    operation: str,
    status: str,
) -> None:
    runtime_state.upsert_record(
        _PUBLISH_RUNTIME_KIND,
        guild_id,
        operation_id,
        _publish_payload(panel, operation=operation),
        status=status,
    )


def _message_has_publish_marker(message: object, operation_id: str) -> bool:
    return message_checkpoints.message_has_checkpoint(
        message,
        _publish_marker(operation_id),
    )


def _message_is_bot_authored(guild: discord.Guild, message: object) -> bool:
    """Verify ownership when Discord exposes the guild's current bot member."""

    bot_user_id = int(getattr(getattr(guild, "me", None), "id", 0) or 0)
    if not bot_user_id:
        return True
    author_id = int(getattr(getattr(message, "author", None), "id", 0) or 0)
    return author_id == bot_user_id


async def _clean_legacy_publish_footers(message: discord.Message) -> bool:
    """Remove old publication footers when their operation record is already gone."""

    await message_checkpoints.clean_message_checkpoint_prefixes(
        message,
        (_PUBLISH_MARKER_PREFIX,),
    )
    return True


async def _clean_committed_panel_checkpoints_for_guild(guild: discord.Guild) -> None:
    """Migrate markers on panels committed before checkpoint cleanup existed."""

    if not int(getattr(getattr(guild, "me", None), "id", 0) or 0):
        return
    entry = _load_guild_entry(guild.id)
    panels = entry.get("panels", {}) if isinstance(entry, dict) else {}
    retained_panels = panels.values() if isinstance(panels, dict) else ()
    for panel in retained_panels:
        if not isinstance(panel, dict):
            continue
        try:
            channel_id = int(panel.get("panel_channel_id") or 0)
            message_id = int(panel.get("panel_message_id") or 0)
        except (TypeError, ValueError):
            continue
        if not channel_id or not message_id:
            continue
        try:
            channel = guild.get_channel(channel_id) or await guild.fetch_channel(channel_id)
            if not isinstance(channel, discord.abc.Messageable):
                continue
            message = await channel.fetch_message(message_id)
            if not _message_is_bot_authored(guild, message):
                continue
            await _clean_legacy_publish_footers(message)
        except discord.NotFound:
            continue
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            logging.warning(
                "Reaction-panel checkpoint still needs cleanup for message %s in guild %s",
                message_id,
                guild.id,
            )


async def _compensate_publish_message(message: discord.Message) -> bool:
    """Delete a non-committed panel, or at least make it visibly inert."""

    try:
        await message.delete()
        return True
    except discord.NotFound:
        return True
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        pass

    try:
        await message.clear_reactions()
        await message.edit(
            content="This incomplete reaction-role panel has been disabled.",
            embed=None,
        )
        return True
    except discord.NotFound:
        return True
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        return False


async def _abort_publish(
    guild_id: int,
    operation_id: str,
    panel: dict,
    message: Optional[discord.Message],
    *,
    operation: str,
) -> None:
    try:
        _record_publish(
            guild_id,
            operation_id,
            panel,
            operation=operation,
            status="cleanup_pending",
        )
    except Exception:
        logging.exception(
            "Could not mark reaction-panel publication %s for cleanup",
            operation_id,
        )

    if message is not None and not await _compensate_publish_message(message):
        return
    try:
        runtime_state.delete_record(
            _PUBLISH_RUNTIME_KIND,
            guild_id,
            operation_id,
        )
    except Exception:
        logging.exception(
            "Could not clear compensated reaction-panel publication %s",
            operation_id,
        )


async def _post_pending_panel(
    guild: discord.Guild,
    destination_channel: discord.TextChannel,
    panel: dict,
    *,
    operation: str,
) -> tuple[discord.Message, str]:
    """Publish only after SQLite can identify and compensate the Discord post.

    The first Discord resource is a harmless placeholder.  A functional embed and
    reactions are added only after its message ID has been durably recorded.
    """

    operation_id = uuid4().hex
    try:
        _record_publish(
            guild.id,
            operation_id,
            panel,
            operation=operation,
            status="prepared",
        )
    except Exception as error:
        raise _PanelPublishError(
            "Local storage is unavailable, so no Discord panel was posted."
        ) from error
    message: Optional[discord.Message] = None
    try:
        marker = _publish_marker(operation_id)
        message = await destination_channel.send(
            content=message_checkpoints.content_with_checkpoint(
                "Preparing reaction-role panel.",
                marker,
            ),
            nonce=message_checkpoints.stable_nonce(marker),
        )
        panel["panel_channel_id"] = int(message.channel.id)
        panel["panel_message_id"] = int(message.id)
        _record_publish(
            guild.id,
            operation_id,
            panel,
            operation=operation,
            status="placeholder_created",
        )
        await message.edit(
            content=None,
            embed=_build_panel_embed(
                str(panel.get("panel_name", "Panel name")),
                str(panel.get("panel_message") or "React to the following emojis to get role."),
                guild,
                panel.get("reactions") or [],
            ),
        )
        for item in panel.get("reactions") or []:
            if not await _add_panel_reaction(
                message,
                str(item.get("emoji", "")),
            ):
                raise _PanelPublishError("Discord rejected one or more configured reactions.")
        _record_publish(
            guild.id,
            operation_id,
            panel,
            operation=operation,
            status="ready_to_commit",
        )
        return message, operation_id
    except Exception as error:
        await _abort_publish(
            guild.id,
            operation_id,
            panel,
            message,
            operation=operation,
        )
        if isinstance(error, _PanelPublishError):
            raise
        raise _PanelPublishError("The panel could not be published or recorded safely.") from error


def _finish_publish(guild_id: int, operation_id: str) -> None:
    try:
        runtime_state.delete_record(
            _PUBLISH_RUNTIME_KIND,
            guild_id,
            operation_id,
        )
    except Exception:
        # The panel configuration is already committed. Startup reconciliation
        # recognizes its durable coordinates and removes this publication record.
        logging.exception(
            "Could not finalize reaction-panel publication %s",
            operation_id,
        )


async def _resolve_pending_publish_message(
    guild: discord.Guild,
    record: runtime_state.RuntimeRecord,
) -> Optional[discord.Message]:
    panel = record.payload.get("panel")
    if not isinstance(panel, dict):
        return None
    try:
        channel_id = int(panel.get("panel_channel_id") or panel.get("destination_channel_id") or 0)
        message_id = int(panel.get("panel_message_id") or 0)
    except (TypeError, ValueError):
        return None
    if not channel_id:
        return None

    cached_channel = guild.get_channel(channel_id)
    message_channel: discord.abc.Messageable
    if cached_channel is None:
        try:
            fetched_channel = await guild.fetch_channel(channel_id)
        except discord.NotFound:
            return None
        if not isinstance(fetched_channel, discord.abc.Messageable):
            return None
        message_channel = fetched_channel
    elif isinstance(cached_channel, discord.abc.Messageable):
        message_channel = cached_channel
    else:
        return None
    if message_id:
        try:
            message = await message_channel.fetch_message(message_id)
        except discord.NotFound:
            return None
        return message if _message_is_bot_authored(guild, message) else None

    # A crash between creating the harmless placeholder and recording its ID can
    # only leave a marked, non-functional message. Scan the one known channel once.
    async for message in message_channel.history(limit=_FALLBACK_HISTORY_LIMIT):
        if _message_is_bot_authored(guild, message) and _message_has_publish_marker(
            message,
            record.external_id,
        ):
            return message
    return None


def _publication_was_committed(record: runtime_state.RuntimeRecord) -> bool:
    panel = record.payload.get("panel")
    if not isinstance(panel, dict):
        return False
    panel_id = str(record.payload.get("panel_id") or panel.get("id") or "")
    if not panel_id:
        return False
    stored = _get_panel_by_id(record.guild_id, panel_id)
    if not isinstance(stored, dict):
        return False
    try:
        return (
            int(stored.get("panel_channel_id") or 0) == int(panel.get("panel_channel_id") or 0)
            and int(stored.get("panel_message_id") or 0) == int(panel.get("panel_message_id") or 0)
            and int(panel.get("panel_message_id") or 0) > 0
        )
    except (TypeError, ValueError):
        return False


async def reconcile_reaction_role_panels(bot: discord.Client) -> None:
    """Compensate interrupted publishes without deleting committed panels."""

    for record in runtime_state.list_records(_PUBLISH_RUNTIME_KIND):
        try:
            if _publication_was_committed(record):
                panel = record.payload.get("panel")
                guild = bot.get_guild(record.guild_id)
                if (
                    str(record.payload.get("operation") or "") == "resend"
                    and isinstance(panel, dict)
                    and guild is not None
                    and not await _disable_previous_reaction_panel_message(guild, panel)
                ):
                    runtime_state.set_status(
                        _PUBLISH_RUNTIME_KIND,
                        record.guild_id,
                        record.external_id,
                        "old_cleanup_pending",
                    )
                    continue
                runtime_state.delete_record(
                    _PUBLISH_RUNTIME_KIND,
                    record.guild_id,
                    record.external_id,
                )
                continue
            guild = bot.get_guild(record.guild_id)
            if guild is None:
                continue
            message = await _resolve_pending_publish_message(guild, record)
            if message is not None and not await _compensate_publish_message(message):
                runtime_state.set_status(
                    _PUBLISH_RUNTIME_KIND,
                    record.guild_id,
                    record.external_id,
                    "cleanup_pending",
                )
                continue
            runtime_state.delete_record(
                _PUBLISH_RUNTIME_KIND,
                record.guild_id,
                record.external_id,
            )
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            logging.warning(
                "Reaction-panel publication %s still needs startup cleanup",
                record.external_id,
            )
        except Exception:
            logging.exception(
                "Reaction-panel publication reconciliation failed for %s",
                record.external_id,
            )

    for guild in getattr(bot, "guilds", ()):
        try:
            await _clean_committed_panel_checkpoints_for_guild(guild)
        except Exception:
            logging.exception(
                "Committed reaction-panel checkpoint cleanup failed for guild %s",
                guild.id,
            )
        try:
            await _reconcile_reaction_assignments_for_guild(guild)
        except Exception:
            logging.exception(
                "Reaction-role membership reconciliation failed for guild %s",
                guild.id,
            )


async def _disable_previous_reaction_panel_message(
    guild: discord.Guild,
    panel: dict,
) -> bool:
    try:
        channel_id = int(panel.get("previous_panel_channel_id") or 0)
        message_id = int(panel.get("previous_panel_message_id") or 0)
    except (TypeError, ValueError):
        return True
    if not channel_id or not message_id:
        return True
    try:
        channel = guild.get_channel(channel_id) or await guild.fetch_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            return False
        message = await channel.fetch_message(message_id)
        await message.clear_reactions()
        await message.edit(
            content="This reaction-role panel has been replaced and is no longer active.",
            embed=None,
        )
    except discord.NotFound:
        return True
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        return False
    return True


def _build_home_embed(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="Role Reaction Setup",
        description="## :gear: Choose an option below to manage role reaction panels.",
    )
    return embed


def _build_setup_embed(view: "RoleReactionSetupView") -> discord.Embed:
    state = view.state
    guild = view.guild
    step = view.step

    embed = discord.Embed(title=f"Role Reaction Panel Setup — Step {step}/5")

    if step == 1:
        embed.description = "## :pencil: Set panel name"
        embed.add_field(name="Panel name", value=state["panel_name"] or "Not set", inline=False)
    elif step == 2:
        embed.description = "## :speech_balloon: Set panel message"
        embed.add_field(name="Panel message", value=state["panel_message"], inline=False)
    elif step == 3:
        embed.description = (
            "## :label: Set emoji → associated role\n"
            f"Up to {_MAX_REACTIONS_PER_PANEL} role reactions."
        )
        embed.add_field(
            name=f"Role reactions ({len(state['reactions'])}/{_MAX_REACTIONS_PER_PANEL})",
            value=_format_role_reaction_list(guild, state["reactions"]),
            inline=False,
        )
    elif step == 4:
        embed.description = "## :satellite: Select destination channel"
        embed.add_field(
            name="Destination channel",
            value=_format_channel_mention(guild, state["destination_channel_id"]),
            inline=False,
        )
    else:
        embed.description = "## :clipboard: Preview and final confirmation"
        embed.add_field(name="Panel name", value=state["panel_name"] or "Not set", inline=False)
        embed.add_field(name="Panel message", value=state["panel_message"], inline=False)
        embed.add_field(
            name=f"Role reactions ({len(state['reactions'])}/{_MAX_REACTIONS_PER_PANEL})",
            value=_format_role_reaction_list(guild, state["reactions"]),
            inline=False,
        )
        embed.add_field(
            name="Destination channel",
            value=_format_channel_mention(guild, state["destination_channel_id"]),
            inline=False,
        )
        embed.add_field(
            name="Panel preview",
            value=f"**{state['panel_name']}**\n{state['panel_message']}",
            inline=False,
        )

    return embed


def _build_picker_embed(picker: "RoleReactionPickerView") -> discord.Embed:
    emoji_display = picker.selected_emoji_raw or "*(not selected)*"
    role_display = (
        f"<@&{picker.selected_role_id}>" if picker.selected_role_id else "*(not selected)*"
    )
    embed = discord.Embed(title="Add Role Reaction")
    embed.add_field(name="Emoji", value=emoji_display, inline=True)
    embed.add_field(name="Role", value=role_display, inline=True)
    return embed


def _build_manage_embed(
    guild: discord.Guild, panels: list[dict], selected_panel_id: Optional[str]
) -> discord.Embed:
    embed = discord.Embed(title="Manage Role Reaction Panels")
    if not panels:
        embed.description = "No role reaction panels configured yet."
        return embed

    selected = next((p for p in panels if str(p.get("id")) == str(selected_panel_id)), panels[0])
    embed.description = "Select a panel to resend it or delete it."
    embed.add_field(name="Panel name", value=selected.get("panel_name", "Unknown"), inline=False)
    embed.add_field(name="Panel message", value=selected.get("panel_message", ""), inline=False)
    embed.add_field(
        name="Role reactions",
        value=_format_role_reaction_list(guild, selected.get("reactions", [])),
        inline=False,
    )
    embed.add_field(
        name="Destination channel",
        value=_format_channel_mention(guild, selected.get("destination_channel_id")),
        inline=False,
    )
    return embed


class PanelNameModal(discord.ui.Modal, title="Set Panel Name"):
    panel_name: discord.ui.TextInput["PanelNameModal"] = discord.ui.TextInput(
        label="Panel name",
        required=True,
        max_length=100,
        default="Panel name",
    )

    def __init__(self, parent_view: "RoleReactionSetupView"):
        super().__init__()
        self.parent_view = parent_view
        self.panel_name.default = parent_view.state["panel_name"]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.parent_view.state["panel_name"] = str(self.panel_name).strip() or "Panel name"
        await interaction.response.edit_message(
            embed=_build_setup_embed(self.parent_view),
            view=self.parent_view,
        )


class PanelMessageModal(discord.ui.Modal, title="Set Panel Message"):
    panel_message: discord.ui.TextInput["PanelMessageModal"] = discord.ui.TextInput(
        label="Panel message",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000,
    )

    def __init__(self, parent_view: "RoleReactionSetupView"):
        super().__init__()
        self.parent_view = parent_view
        self.panel_message.default = parent_view.state["panel_message"]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.parent_view.state["panel_message"] = (
            str(self.panel_message).strip() or "React to the following emojis to get role."
        )
        await interaction.response.edit_message(
            embed=_build_setup_embed(self.parent_view),
            view=self.parent_view,
        )


class _EmojiInputModal(discord.ui.Modal, title="Select Emoji"):
    emoji_input: discord.ui.TextInput["_EmojiInputModal"] = discord.ui.TextInput(
        label="Paste or type an emoji",
        placeholder="e.g. 🎮 or :gear:",
        required=True,
        max_length=50,
    )

    def __init__(self, picker: "RoleReactionPickerView"):
        super().__init__()
        self._picker = picker
        if picker.selected_emoji_raw:
            self.emoji_input.default = picker.selected_emoji_raw

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self._picker.selected_emoji_raw = str(self.emoji_input).strip()
        self._picker._build_items()
        await interaction.response.edit_message(
            embed=_build_picker_embed(self._picker), view=self._picker
        )


class RoleReactionPickerView(discord.ui.View):
    def __init__(self, parent_view: "RoleReactionSetupView", nonce: int):
        super().__init__(timeout=300)
        self.parent_view = parent_view
        self._nonce = nonce
        self.selected_emoji_raw: Optional[str] = None
        self.selected_role_id: Optional[int] = None
        self._build_items()

    def _build_items(self) -> None:
        self.clear_items()
        self.add_item(_PickerBackButton(self))
        self.add_item(_EmojiSelectButton(self))
        self.add_item(_RolePickerSelect(custom_id=f"rr-role-{self._nonce}"))
        self.add_item(_SaveReactionButton(self))


class _PickerBackButton(discord.ui.Button):
    def __init__(self, picker: RoleReactionPickerView):
        super().__init__(
            label="Back",
            style=discord.ButtonStyle.secondary,
            custom_id=f"rr-pick-back-{picker._nonce}",
        )
        self._picker = picker

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._picker.parent_view.user_id:
            return
        parent = self._picker.parent_view
        parent._build_items()
        await interaction.response.edit_message(embed=_build_setup_embed(parent), view=parent)


class _EmojiSelectButton(discord.ui.Button):
    def __init__(self, picker: RoleReactionPickerView):
        label = (
            f"Selected emoji: {picker.selected_emoji_raw}"
            if picker.selected_emoji_raw
            else "Select emoji"
        )
        super().__init__(
            label=label,
            style=discord.ButtonStyle.primary,
            custom_id=f"rr-emoji-btn-{picker._nonce}",
        )
        self._picker = picker

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._picker.parent_view.user_id:
            return
        await interaction.response.send_modal(_EmojiInputModal(self._picker))


class _RolePickerSelect(discord.ui.RoleSelect):
    def __init__(self, custom_id: str):
        super().__init__(
            placeholder="Select role",
            min_values=1,
            max_values=1,
            custom_id=custom_id,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, RoleReactionPickerView):
            return
        if interaction.user.id != view.parent_view.user_id:
            return
        role = self.values[0]
        role_error = role_security.self_assignment_error(
            role,
            view.parent_view.guild,
        )
        if role_error:
            await _send_ephemeral_notice(interaction, role_error)
            return
        view.selected_role_id = role.id
        view._build_items()
        await interaction.response.edit_message(embed=_build_picker_embed(view), view=view)


class _SaveReactionButton(discord.ui.Button):
    def __init__(self, picker: RoleReactionPickerView):
        super().__init__(
            label="Save", style=discord.ButtonStyle.success, custom_id=f"rr-save-{picker._nonce}"
        )
        self._picker = picker

    async def callback(self, interaction: discord.Interaction) -> None:
        picker = self._picker
        if interaction.user.id != picker.parent_view.user_id:
            return

        if not picker.selected_emoji_raw or not picker.selected_role_id:
            await _send_ephemeral_notice(interaction, "Select an emoji and a role first.")
            return

        normalized_emoji = _normalize_emoji_input(picker.selected_emoji_raw)
        if normalized_emoji is None:
            await _send_ephemeral_notice(
                interaction,
                "Invalid emoji. Use a standard Unicode emoji (like ⚙️) or a shortcode like `:gear:`.",
            )
            return

        state = picker.parent_view.state
        if len(state["reactions"]) >= _MAX_REACTIONS_PER_PANEL:
            await _send_ephemeral_notice(
                interaction,
                f"Panels can have at most {_MAX_REACTIONS_PER_PANEL} role reactions.",
            )
            return

        for existing in state["reactions"]:
            if _emoji_matches(str(existing.get("emoji", "")), normalized_emoji):
                await _send_ephemeral_notice(
                    interaction, "That emoji is already used in this panel."
                )
                return
            if int(existing.get("role_id", 0) or 0) == int(picker.selected_role_id):
                await _send_ephemeral_notice(
                    interaction, "That role is already used in this panel."
                )
                return

        state["reactions"].append({"emoji": normalized_emoji, "role_id": picker.selected_role_id})

        parent = picker.parent_view
        parent._build_items()
        await interaction.response.edit_message(embed=_build_setup_embed(parent), view=parent)


class RoleReactionSetupView(discord.ui.View):
    def __init__(
        self,
        guild: discord.Guild,
        user_id: int,
        step: int = 1,
        state: Optional[dict] = None,
    ):
        super().__init__(timeout=900)
        self.guild = guild
        self.user_id = user_id
        self.step = step
        self.state = state or {
            "panel_name": "Panel name",
            "panel_message": "React to the following emojis to get role.",
            "reactions": [],
            "destination_channel_id": None,
        }
        self._nonce = 0
        self._build_items()

    async def ensure_owner(self, interaction: discord.Interaction) -> bool:
        return await _ensure_current_admin_owner(
            interaction,
            self.user_id,
            self.guild,
        )

    def _build_items(self) -> None:
        self._nonce += 1
        self.clear_items()

        if self.step > 1:
            self.add_item(_BackButton())

        if self.step == 1:
            self.add_item(_SetPanelNameButton())
            self.add_item(_ContinueButton())
            self.add_item(_CancelSetupButton())
        elif self.step == 2:
            self.add_item(_SetPanelMessageButton())
            self.add_item(_ContinueButton())
        elif self.step == 3:
            self.add_item(_AddRoleReactionButton())
            self.add_item(_ContinueButton())
        elif self.step == 4:
            self.add_item(_DestinationChannelSelect(custom_id=f"rr-dest-{self._nonce}"))
            self.add_item(_ContinueButton())
        else:
            self.add_item(_ConfirmAndSendButton())

    def next_step(self) -> None:
        self.step = min(5, self.step + 1)
        self._build_items()

    def previous_step(self) -> None:
        self.step = max(1, self.step - 1)
        self._build_items()


class _BackButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Back", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, RoleReactionSetupView):
            return
        if not await view.ensure_owner(interaction):
            return
        view.previous_step()
        await interaction.response.edit_message(embed=_build_setup_embed(view), view=view)


class _CancelSetupButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Cancel", style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, RoleReactionSetupView):
            return
        if not await view.ensure_owner(interaction):
            return

        home_view = RoleReactionHomeView(view.user_id, view.guild)
        view.stop()
        await interaction.response.edit_message(embed=_build_home_embed(view.guild), view=home_view)


class _SetPanelNameButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Set panel name", style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, RoleReactionSetupView):
            return
        if not await view.ensure_owner(interaction):
            return
        await interaction.response.send_modal(PanelNameModal(view))


class _SetPanelMessageButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Set panel message", style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, RoleReactionSetupView):
            return
        if not await view.ensure_owner(interaction):
            return
        await interaction.response.send_modal(PanelMessageModal(view))


class _AddRoleReactionButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Add role reaction", style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, RoleReactionSetupView):
            return
        if not await view.ensure_owner(interaction):
            return

        picker = RoleReactionPickerView(view, view._nonce)
        await interaction.response.edit_message(embed=_build_picker_embed(picker), view=picker)


class _ContinueButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Save and Continue", style=discord.ButtonStyle.success)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, RoleReactionSetupView):
            return
        if not await view.ensure_owner(interaction):
            return

        async def _error(text: str) -> None:
            await _send_ephemeral_notice(interaction, text)

        if view.step == 1 and not str(view.state.get("panel_name", "")).strip():
            await _error("Please set a panel name.")
            return
        if view.step == 3 and not view.state.get("reactions"):
            await _error("Please add at least one role reaction.")
            return
        if view.step == 4 and not view.state.get("destination_channel_id"):
            await _error("Please select a destination channel.")
            return

        view.next_step()
        await interaction.response.edit_message(embed=_build_setup_embed(view), view=view)


class _DestinationChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, custom_id: str):
        super().__init__(
            placeholder="Select destination channel",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
            custom_id=custom_id,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, RoleReactionSetupView):
            return
        if not await view.ensure_owner(interaction):
            return
        view.state["destination_channel_id"] = self.values[0].id
        await interaction.response.edit_message(embed=_build_setup_embed(view), view=view)


class _ConfirmAndSendButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Confirm and Send Panel", style=discord.ButtonStyle.success)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, RoleReactionSetupView):
            return
        if not await view.ensure_owner(interaction):
            return
        if not isinstance(interaction.user, discord.Member) or not await authorization.is_admin(
            interaction.user
        ):
            await _send_ephemeral_notice(
                interaction,
                "Your Administrator permission was removed; the panel was not created.",
            )
            return

        async def _show_error(error_text: str) -> None:
            await _send_ephemeral_notice(interaction, error_text)

        if not guild_settings.get_target_guild(view.guild.id):
            await _show_error("This server is no longer configured. Run **/bot-setup** first.")
            return

        state = view.state
        destination_channel = view.guild.get_channel(int(state.get("destination_channel_id") or 0))
        if not isinstance(destination_channel, discord.TextChannel):
            await _show_error("Destination channel not found. Go back and reselect it.")
            return

        bot_member: Optional[discord.Member] = getattr(view.guild, "me", None)
        if bot_member is None:
            await _show_error("Bot member information unavailable. Please try again.")
            return

        perms = destination_channel.permissions_for(bot_member)
        if not (
            perms.view_channel and perms.send_messages and perms.embed_links and perms.add_reactions
        ):
            await _show_error(
                f"I need **View Channel**, **Send Messages**, **Embed Links**, and **Add Reactions** permissions in "
                f"{destination_channel.mention}."
            )
            return

        reactions = state.get("reactions") or []
        if not reactions:
            await _show_error("Please add at least one role reaction.")
            return
        for item in reactions:
            role = view.guild.get_role(int(item.get("role_id", 0) or 0))
            role_error = role_security.self_assignment_error(
                role,
                view.guild,
            )
            if role_error:
                await _show_error(f"Role reaction configuration error: {role_error}")
                return

        await interaction.response.defer()

        panel_id = uuid4().hex[:10]
        panel = {
            "id": panel_id,
            "panel_name": state.get("panel_name", "Panel name"),
            "panel_message": state.get(
                "panel_message", "React to the following emojis to get role."
            ),
            "reactions": reactions,
            "destination_channel_id": int(state.get("destination_channel_id") or 0),
        }
        try:
            panel_message, publish_operation_id = await _post_pending_panel(
                view.guild,
                destination_channel,
                panel,
                operation="create",
            )
        except _PanelPublishError as error:
            await _show_error(str(error))
            return

        if not guild_settings.get_target_guild(view.guild.id):
            await _abort_publish(
                view.guild.id,
                publish_operation_id,
                panel,
                panel_message,
                operation="create",
            )
            await _show_error("The bot setup was removed while this panel was being posted.")
            return
        if not await _ensure_current_admin_owner(
            interaction,
            view.user_id,
            view.guild,
        ):
            await _abort_publish(
                view.guild.id,
                publish_operation_id,
                panel,
                panel_message,
                operation="create",
            )
            return
        if not guild_settings.get_target_guild(view.guild.id):
            await _abort_publish(
                view.guild.id,
                publish_operation_id,
                panel,
                panel_message,
                operation="create",
            )
            await _show_error("The bot setup was removed while this panel was being posted.")
            return
        for item in reactions:
            role = view.guild.get_role(int(item.get("role_id", 0) or 0))
            role_error = role_security.self_assignment_error(role, view.guild)
            if role_error:
                await _abort_publish(
                    view.guild.id,
                    publish_operation_id,
                    panel,
                    panel_message,
                    operation="create",
                )
                await _show_error(
                    f"Role reaction configuration changed while posting: {role_error}"
                )
                return

        try:
            _save_panel(view.guild.id, panel)
        except Exception:
            await _abort_publish(
                view.guild.id,
                publish_operation_id,
                panel,
                panel_message,
                operation="create",
            )
            await _show_error(
                "The panel could not be saved locally, so its Discord message was disabled."
            )
            return
        _finish_publish(view.guild.id, publish_operation_id)

        home_view = RoleReactionHomeView(view.user_id, view.guild)
        if interaction.message is not None:
            try:
                await interaction.message.edit(embed=_build_home_embed(view.guild), view=home_view)
            except discord.HTTPException:
                pass


class _ManagePanelSelect(discord.ui.Select):
    def __init__(self, panels: list[dict], selected_id: Optional[str]):
        options = [
            discord.SelectOption(
                label=str(p.get("panel_name", "Panel"))[:100],
                value=str(p.get("id")),
                default=str(p.get("id")) == str(selected_id),
            )
            for p in panels
        ]
        super().__init__(placeholder="Select panel", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ManagePanelsView):
            return
        if not await _ensure_current_admin_owner(
            interaction,
            view.user_id,
            view.guild,
        ):
            return
        view.selected_panel_id = self.values[0]
        view._build_items()
        await interaction.response.edit_message(
            embed=_build_manage_embed(view.guild, view.panels, view.selected_panel_id),
            view=view,
        )


class ManagePanelsView(discord.ui.View):
    def __init__(
        self,
        guild: discord.Guild,
        user_id: int,
        panels: list[dict],
        selected_id: Optional[str] = None,
        page: int = 0,
    ):
        super().__init__(timeout=900)
        self.guild = guild
        self.user_id = user_id
        self.panels = panels
        self.selected_panel_id = selected_id or (str(panels[0].get("id")) if panels else None)
        self.page = max(0, int(page))
        if self.selected_panel_id:
            for index, panel in enumerate(self.panels):
                if str(panel.get("id")) == str(self.selected_panel_id):
                    self.page = index // _PANEL_PAGE_SIZE
                    break
        self._build_items()

    @property
    def page_count(self) -> int:
        return max(1, (len(self.panels) + _PANEL_PAGE_SIZE - 1) // _PANEL_PAGE_SIZE)

    def _visible_panels(self) -> list[dict]:
        self.page = min(self.page, self.page_count - 1)
        start = self.page * _PANEL_PAGE_SIZE
        return self.panels[start : start + _PANEL_PAGE_SIZE]

    def _get_selected_panel(self) -> Optional[dict]:
        if self.selected_panel_id is None:
            return None
        return _get_panel_by_id(self.guild.id, str(self.selected_panel_id))

    def _build_items(self) -> None:
        self.clear_items()
        if self.panels:
            visible = self._visible_panels()
            if not any(str(panel.get("id")) == str(self.selected_panel_id) for panel in visible):
                self.selected_panel_id = str(visible[0].get("id")) if visible else None
            self.add_item(_ManagePanelSelect(visible, self.selected_panel_id))
            self.add_item(_SendPanelAgainButton())
            self.add_item(_DeletePanelButton())
            if self.page_count > 1:
                self.add_item(_PreviousPanelsPageButton(disabled=self.page <= 0))
                self.add_item(_NextPanelsPageButton(disabled=self.page >= self.page_count - 1))
        self.add_item(_ManageBackButton())


class _PreviousPanelsPageButton(discord.ui.Button):
    def __init__(self, *, disabled: bool):
        super().__init__(label="Previous", style=discord.ButtonStyle.secondary, disabled=disabled)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ManagePanelsView) or not await _ensure_current_admin_owner(
            interaction, view.user_id, view.guild
        ):
            return
        view.page = max(0, view.page - 1)
        view.selected_panel_id = None
        view._build_items()
        await interaction.response.edit_message(
            embed=_build_manage_embed(view.guild, view.panels, view.selected_panel_id),
            view=view,
        )


class _NextPanelsPageButton(discord.ui.Button):
    def __init__(self, *, disabled: bool):
        super().__init__(label="Next", style=discord.ButtonStyle.secondary, disabled=disabled)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ManagePanelsView) or not await _ensure_current_admin_owner(
            interaction, view.user_id, view.guild
        ):
            return
        view.page = min(view.page_count - 1, view.page + 1)
        view.selected_panel_id = None
        view._build_items()
        await interaction.response.edit_message(
            embed=_build_manage_embed(view.guild, view.panels, view.selected_panel_id),
            view=view,
        )


class _SendPanelAgainButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Send panel again", style=discord.ButtonStyle.success)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ManagePanelsView):
            return
        if not await _ensure_current_admin_owner(
            interaction,
            view.user_id,
            view.guild,
        ):
            return

        panel = view._get_selected_panel()
        if panel is None:
            return

        destination_channel_id = int(panel.get("destination_channel_id", 0) or 0)
        destination_channel = view.guild.get_channel(destination_channel_id)
        if not isinstance(destination_channel, discord.TextChannel):
            await _send_ephemeral_notice(
                interaction,
                "Destination channel not found. You can delete and recreate this panel.",
            )
            return

        bot_member: Optional[discord.Member] = getattr(view.guild, "me", None)
        if bot_member is None:
            await _send_ephemeral_notice(
                interaction, "Bot member information unavailable. Please try again."
            )
            return

        perms = destination_channel.permissions_for(bot_member)
        if not (
            perms.view_channel and perms.send_messages and perms.embed_links and perms.add_reactions
        ):
            await _send_ephemeral_notice(
                interaction,
                (
                    "I need **View Channel**, **Send Messages**, **Embed Links**, and **Add Reactions** permissions in "
                    f"{destination_channel.mention}."
                ),
            )
            return

        await interaction.response.defer()

        candidate_panel = dict(panel)
        candidate_panel["previous_panel_channel_id"] = int(panel.get("panel_channel_id") or 0)
        candidate_panel["previous_panel_message_id"] = int(panel.get("panel_message_id") or 0)
        # The pending record must describe only the new Discord publication. Keeping
        # the currently-active message IDs here could make startup mistake an
        # interrupted resend for an already-committed operation.
        candidate_panel.pop("panel_channel_id", None)
        candidate_panel.pop("panel_message_id", None)
        try:
            panel_message, publish_operation_id = await _post_pending_panel(
                view.guild,
                destination_channel,
                candidate_panel,
                operation="resend",
            )
        except _PanelPublishError as error:
            await _send_ephemeral_notice(interaction, str(error))
            return

        if not await _ensure_current_admin_owner(
            interaction,
            view.user_id,
            view.guild,
        ):
            await _abort_publish(
                view.guild.id,
                publish_operation_id,
                candidate_panel,
                panel_message,
                operation="resend",
            )
            return
        fresh_panel = _get_panel_by_id(view.guild.id, str(panel.get("id")))
        if fresh_panel is None:
            await _abort_publish(
                view.guild.id,
                publish_operation_id,
                candidate_panel,
                panel_message,
                operation="resend",
            )
            await _send_ephemeral_notice(
                interaction,
                "This panel was removed while it was being sent.",
            )
            return
        fresh_panel["panel_channel_id"] = candidate_panel["panel_channel_id"]
        fresh_panel["panel_message_id"] = candidate_panel["panel_message_id"]
        try:
            _save_panel(view.guild.id, fresh_panel)
        except Exception:
            await _abort_publish(
                view.guild.id,
                publish_operation_id,
                candidate_panel,
                panel_message,
                operation="resend",
            )
            await _send_ephemeral_notice(
                interaction,
                "The panel could not be saved locally, so its new Discord message was disabled.",
            )
            return
        old_message_disabled = await _disable_previous_reaction_panel_message(
            view.guild,
            candidate_panel,
        )
        if old_message_disabled:
            _finish_publish(view.guild.id, publish_operation_id)
        else:
            _record_publish(
                view.guild.id,
                publish_operation_id,
                candidate_panel,
                operation="resend",
                status="old_cleanup_pending",
            )
        view.panels = _list_panels(view.guild.id)
        view._build_items()

        if interaction.message is not None:
            await interaction.message.edit(
                embed=_build_manage_embed(view.guild, view.panels, view.selected_panel_id),
                view=view,
            )
        await _send_ephemeral_notice(interaction, f"Panel sent to {destination_channel.mention}.")


class _DeletePanelButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Delete panel", style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ManagePanelsView):
            return
        if not await _ensure_current_admin_owner(
            interaction,
            view.user_id,
            view.guild,
        ):
            return

        panel = view._get_selected_panel()
        if panel is None:
            return

        await interaction.response.defer()
        channel_id = int(panel.get("panel_channel_id", 0) or 0)
        message_id = int(panel.get("panel_message_id", 0) or 0)
        target_channel = view.guild.get_channel(channel_id)
        if isinstance(target_channel, discord.TextChannel) and message_id:
            try:
                msg = await target_channel.fetch_message(message_id)
                if not await _ensure_current_admin_owner(
                    interaction,
                    view.user_id,
                    view.guild,
                ):
                    return
                await msg.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        if not await _ensure_current_admin_owner(
            interaction,
            view.user_id,
            view.guild,
        ):
            return
        if _get_panel_by_id(view.guild.id, str(panel.get("id"))) is None:
            await _send_ephemeral_notice(
                interaction,
                "This panel has already been removed.",
            )
            return

        _delete_panel(view.guild.id, str(panel.get("id")))
        view.panels = _list_panels(view.guild.id)
        view.selected_panel_id = str(view.panels[0].get("id")) if view.panels else None
        view._build_items()
        if interaction.message is not None:
            await interaction.message.edit(
                embed=_build_manage_embed(view.guild, view.panels, view.selected_panel_id),
                view=view,
            )
        await _send_ephemeral_notice(interaction, "Panel deleted.")


class _ManageBackButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Back", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ManagePanelsView):
            return
        if not await _ensure_current_admin_owner(
            interaction,
            view.user_id,
            view.guild,
        ):
            return
        home_view = RoleReactionHomeView(view.user_id, view.guild)
        await interaction.response.edit_message(embed=_build_home_embed(view.guild), view=home_view)


class RoleReactionHomeView(discord.ui.View):
    def __init__(self, user_id: int, guild: discord.Guild):
        super().__init__(timeout=900)
        self.user_id = user_id
        self.guild = guild
        self.add_item(_CreatePanelButton())
        self.add_item(_OpenManagePanelsButton())


class _CreatePanelButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Create new panel", style=discord.ButtonStyle.success)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, RoleReactionHomeView):
            return
        if not await _ensure_current_admin_owner(
            interaction,
            view.user_id,
            view.guild,
        ):
            return
        setup_view = RoleReactionSetupView(view.guild, view.user_id)
        await interaction.response.edit_message(
            embed=_build_setup_embed(setup_view), view=setup_view
        )


class _OpenManagePanelsButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Manage panels", style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, RoleReactionHomeView):
            return
        if not await _ensure_current_admin_owner(
            interaction,
            view.user_id,
            view.guild,
        ):
            return
        panels = _list_panels(view.guild.id)
        manage_view = ManagePanelsView(view.guild, view.user_id, panels)
        await interaction.response.edit_message(
            embed=_build_manage_embed(view.guild, panels, manage_view.selected_panel_id),
            view=manage_view,
        )


def _find_role_id_for_emoji(reactions: list[dict], emoji_str: str) -> Optional[int]:
    for item in reactions:
        if _emoji_matches(str(item.get("emoji", "")), emoji_str):
            role_id = item.get("role_id")
            if role_id is None:
                return None
            try:
                return int(role_id)
            except (TypeError, ValueError):
                return None
    return None


def _panel_assignment_version(panel: dict) -> str:
    material = (
        int(panel.get("panel_message_id") or 0),
        tuple(
            sorted(
                (
                    _emoji_key(str(item.get("emoji") or "")),
                    int(item.get("role_id") or 0),
                )
                for item in panel.get("reactions", [])
                if isinstance(item, dict)
            )
        ),
    )
    return hashlib.sha256(repr(material).encode("utf-8")).hexdigest()


async def _apply_member_role_state(
    member,
    role,
    *,
    desired: bool,
) -> bool:
    lock_key = (int(member.guild.id), int(member.id), int(role.id))
    async with _member_role_locks.hold(lock_key):
        has_role = role in getattr(member, "roles", ())
        try:
            if desired and not has_role:
                await member.add_roles(role, reason="Reaction-role state reconciliation")
            elif not desired and has_role:
                await member.remove_roles(role, reason="Reaction-role state reconciliation")
        except (discord.Forbidden, discord.HTTPException):
            return False
    return True


async def _reconcile_reaction_assignments_for_guild(guild: discord.Guild) -> None:
    """Repair reaction/role drift that happened while the bot was offline."""

    if not guild_settings.get_target_guild(guild.id):
        return
    active_cache_keys: set[tuple[int, int]] = set()
    for panel in _list_panels(guild.id):
        try:
            channel_id = int(panel.get("panel_channel_id") or 0)
            message_id = int(panel.get("panel_message_id") or 0)
        except (TypeError, ValueError):
            continue
        if not channel_id or not message_id:
            continue
        cache_key = (int(guild.id), message_id)
        active_cache_keys.add(cache_key)
        version = _panel_assignment_version(panel)
        if _offline_reconciled_panel_versions.get(cache_key) == version:
            continue
        try:
            channel = guild.get_channel(channel_id) or await guild.fetch_channel(channel_id)
            if not isinstance(channel, discord.abc.Messageable):
                continue
            message = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
            continue

        panel_succeeded = True
        for mapping in panel.get("reactions", []) or []:
            if not isinstance(mapping, dict):
                continue
            role = guild.get_role(int(mapping.get("role_id") or 0))
            if role_security.self_assignment_error(role, guild):
                panel_succeeded = False
                continue
            reaction = next(
                (
                    candidate
                    for candidate in getattr(message, "reactions", ()) or ()
                    if _emoji_matches(
                        str(getattr(candidate, "emoji", "")),
                        str(mapping.get("emoji") or ""),
                    )
                ),
                None,
            )
            desired_members: dict[int, discord.Member] = {}
            reaction_is_complete = True
            if reaction is not None:
                reaction_is_complete = int(getattr(reaction, "count", 0) or 0) <= (
                    _MAX_OFFLINE_RECONCILIATION_USERS + 1
                )
                try:
                    async for user in reaction.users(limit=_MAX_OFFLINE_RECONCILIATION_USERS):
                        if getattr(user, "bot", False):
                            continue
                        member = guild.get_member(int(user.id))
                        if member is None:
                            try:
                                member = await guild.fetch_member(int(user.id))
                            except (
                                discord.NotFound,
                                discord.Forbidden,
                                discord.HTTPException,
                            ):
                                continue
                        desired_members[int(member.id)] = member
                except (discord.Forbidden, discord.HTTPException, AttributeError):
                    panel_succeeded = False
                    continue

            for member in desired_members.values():
                if not await _apply_member_role_state(member, role, desired=True):
                    panel_succeeded = False
            if reaction_is_complete:
                for member in tuple(getattr(role, "members", ()) or ()):
                    if getattr(member, "bot", False) or int(member.id) in desired_members:
                        continue
                    if not await _apply_member_role_state(member, role, desired=False):
                        panel_succeeded = False
        if panel_succeeded:
            _offline_reconciled_panel_versions[cache_key] = version

    for cache_key in tuple(_offline_reconciled_panel_versions):
        if cache_key[0] == int(guild.id) and cache_key not in active_cache_keys:
            _offline_reconciled_panel_versions.pop(cache_key, None)


async def _handle_raw_reaction_role_event(
    bot,
    payload: discord.RawReactionActionEvent,
    *,
    desired: bool,
) -> None:
    if payload.guild_id is None or payload.user_id == getattr(bot.user, "id", None):
        return
    guild = bot.get_guild(payload.guild_id)
    if guild is None or not guild_settings.get_target_guild(guild.id):
        return
    panel = _get_panel_by_message_id(guild.id, payload.message_id)
    if panel is None:
        return
    matching_role_id = _find_role_id_for_emoji(
        panel.get("reactions", []),
        str(payload.emoji),
    )
    if matching_role_id is None:
        return
    role = guild.get_role(int(matching_role_id))
    if role_security.self_assignment_error(role, guild):
        return

    lock_key = (int(guild.id), int(payload.user_id), int(matching_role_id))
    async with _member_role_locks.hold(lock_key):
        # Re-read after waiting: removal/setup can disable a panel while an older
        # gateway event is queued.
        if not guild_settings.get_target_guild(guild.id):
            return
        fresh_panel = _get_panel_by_message_id(guild.id, payload.message_id)
        if (
            fresh_panel is None
            or _find_role_id_for_emoji(
                fresh_panel.get("reactions", []),
                str(payload.emoji),
            )
            != matching_role_id
        ):
            return
        try:
            member = guild.get_member(payload.user_id) or await guild.fetch_member(payload.user_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return
        if member is None or getattr(member, "bot", False):
            return
        try:
            # Raw events are already state transitions. Apply each transition while
            # holding the ordered member/role lock instead of trusting a possibly
            # stale Member.roles cache to suppress it.
            if desired:
                await member.add_roles(role, reason="Role reaction panel")
            else:
                await member.remove_roles(role, reason="Role reaction panel")
        except (discord.Forbidden, discord.HTTPException) as err:
            logging.warning(
                "Could not %s role %s for member %s: %s",
                "add" if desired else "remove",
                role.name,
                member.id,
                err,
            )


async def handle_raw_reaction_add(bot, payload: discord.RawReactionActionEvent) -> None:
    await _handle_raw_reaction_role_event(bot, payload, desired=True)


async def handle_raw_reaction_remove(bot, payload: discord.RawReactionActionEvent) -> None:
    await _handle_raw_reaction_role_event(bot, payload, desired=False)


async def handle_role_reaction_setup(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used inside a server.",
            ephemeral=True,
        )
        return

    if not isinstance(interaction.user, discord.Member) or not await authorization.is_admin(
        interaction.user
    ):
        await interaction.response.send_message(
            "You don't have permission to use this command.",
            ephemeral=True,
        )
        return
    if not guild_settings.get_target_guild(interaction.guild.id):
        await interaction.response.send_message(
            "This server is not configured yet. Run **/bot-setup** first.",
            ephemeral=True,
        )
        return

    home_view = RoleReactionHomeView(interaction.user.id, interaction.guild)
    await interaction.response.send_message(
        embed=_build_home_embed(interaction.guild), view=home_view
    )
