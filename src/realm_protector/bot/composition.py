import logging
import re
from typing import Optional
from uuid import uuid4

import discord
from discord.ext import commands

from src.realm_protector.bot import message_checkpoints
from src.realm_protector.bot.common import allowed_user_mentions
from src.realm_protector.infrastructure import guild_settings, runtime_state
from src.realm_protector.services.keyed_locks import KeyedLockPool
from src.realm_protector.services.role_security import (
    member_has_safe_privileged_role,
)

_message_locks: KeyedLockPool[int] = KeyedLockPool()
_creation_locks: KeyedLockPool[tuple[int, str]] = KeyedLockPool()
MAX_PARTIES_PER_COMMAND = 10
MAX_LINES_PER_PARTY = 30
_USER_MENTION_PATTERN = re.compile(r"<@!?([0-9]+)>")
LOGGER = logging.getLogger(__name__)
_RUNTIME_KIND = "composition_party"
_CREATION_RUNTIME_KIND = "composition_creation"
_CREATION_MARKER_PREFIX = "Realm Protector composition creation"
_CREATION_HISTORY_LIMIT = None
_MESSAGE_CHECKPOINT_CLEANUP_FIELD = "message_checkpoint_removed"


def _creation_marker(operation_id: str, party_index: int) -> str:
    return f"{_CREATION_MARKER_PREFIX}:{operation_id}:{int(party_index)}"


def _message_has_creation_marker(
    message: object,
    operation_id: str,
    party_index: int,
) -> bool:
    marker = _creation_marker(operation_id, party_index)
    return message_checkpoints.message_has_checkpoint(message, marker)


def _checkpointed_party_content(content: str, marker: str) -> str:
    """Keep the full party text whenever it fits beside the temporary token."""

    checkpoint = message_checkpoints.hidden_checkpoint(marker)
    if len(content) + len(checkpoint) <= 2000:
        return message_checkpoints.content_with_checkpoint(content, marker)
    # A source Discord message can already use all 2,000 characters. In that
    # case post an invisible placeholder, persist its ID, then restore the full
    # party content after the crash window has closed.
    return message_checkpoints.content_with_checkpoint(None, marker)


async def _clean_party_creation_checkpoint(
    party_message: discord.Message,
    operation_id: str,
    party_index: int,
    *,
    desired_content: Optional[str] = None,
) -> None:
    marker = _creation_marker(operation_id, party_index)
    visible_content = message_checkpoints.strip_checkpoint(
        getattr(party_message, "content", ""),
        marker,
    )
    await message_checkpoints.clean_message_checkpoint(party_message, marker)
    if desired_content is not None and not visible_content:
        await party_message.edit(
            content=desired_content,
            allowed_mentions=discord.AllowedMentions.none(),
        )


def _save_creation_record(
    guild_id: int,
    operation_id: str,
    payload: dict,
    *,
    status: str,
) -> runtime_state.RuntimeRecord:
    return runtime_state.upsert_record(
        _CREATION_RUNTIME_KIND,
        guild_id,
        operation_id,
        payload,
        status=status,
    )


def _persist_party_message(
    party_message,
    content: str,
    *,
    thread_id: Optional[int] = None,
    source_message_id: Optional[int] = None,
) -> None:
    """Persist a bot-authored party message when real Discord IDs are available."""

    guild = getattr(party_message, "guild", None)
    message_id = getattr(party_message, "id", None)
    if guild is None or not getattr(guild, "id", None) or not message_id:
        return
    existing = runtime_state.get_record(_RUNTIME_KIND, guild.id, message_id)
    payload = dict(existing.payload) if existing is not None else {}
    payload.update(
        {
            "starter_message_id": int(message_id),
            "channel_id": int(getattr(getattr(party_message, "channel", None), "id", 0) or 0),
            "content": str(content),
        }
    )
    if thread_id is not None:
        payload["thread_id"] = int(thread_id)
    if source_message_id is not None:
        payload["source_message_id"] = int(source_message_id)
    runtime_state.upsert_record(
        _RUNTIME_KIND,
        guild.id,
        message_id,
        payload,
        status="active",
    )


async def _find_pending_party_message(
    channel,
    operation_id: str,
    party_index: int,
    message_id: Optional[int],
    *,
    bot_user_id: int = 0,
):
    if not bot_user_id:
        bot_user_id = int(
            getattr(
                getattr(getattr(channel, "guild", None), "me", None),
                "id",
                0,
            )
            or 0
        )

    def is_bot_authored(message: object) -> bool:
        if not bot_user_id:
            return True
        return int(getattr(getattr(message, "author", None), "id", 0) or 0) == bot_user_id

    if message_id:
        try:
            message = await channel.fetch_message(int(message_id))
        except discord.NotFound:
            message = None
        # Once SQLite has the Discord ID it is the authoritative identity. The
        # remote checkpoint is needed only for the send-before-ID crash window.
        if message is not None and is_bot_authored(message):
            return message
    async for message in channel.history(limit=_CREATION_HISTORY_LIMIT):
        if is_bot_authored(message) and _message_has_creation_marker(
            message,
            operation_id,
            party_index,
        ):
            return message
    return None


async def _resolve_party_thread(guild, party_message, thread_id: Optional[int]):
    thread = getattr(party_message, "thread", None)
    if isinstance(thread, discord.Thread):
        return thread
    possible_ids = []
    if thread_id:
        possible_ids.append(int(thread_id))
    message_id = int(getattr(party_message, "id", 0) or 0)
    if message_id and message_id not in possible_ids:
        possible_ids.append(message_id)
    for possible_id in possible_ids:
        try:
            candidate = await _fetch_guild_channel(guild, possible_id)
        except discord.NotFound:
            continue
        if isinstance(candidate, discord.Thread):
            return candidate
    return None


async def _resume_composition_creation(
    bot,
    guild,
    record: runtime_state.RuntimeRecord,
    *,
    destination_channel=None,
    send_message=None,
) -> bool:
    lock_key = (int(guild.id), str(record.external_id))
    async with _creation_locks.hold(lock_key):
        return await _resume_composition_creation_locked(
            bot,
            guild,
            record,
            destination_channel=destination_channel,
            send_message=send_message,
        )


async def _resume_composition_creation_locked(
    bot,
    guild,
    record: runtime_state.RuntimeRecord,
    *,
    destination_channel=None,
    send_message=None,
) -> bool:
    """Converge one composition intent after any process/API interruption."""

    payload = dict(record.payload)
    operation_id = str(record.external_id)
    try:
        destination_channel_id = int(payload.get("destination_channel_id") or 0)
    except (TypeError, ValueError):
        destination_channel_id = 0
    if destination_channel is None and destination_channel_id:
        try:
            destination_channel = await _fetch_guild_channel(
                guild,
                destination_channel_id,
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
            return False
    if destination_channel is None:
        return False

    parties = payload.get("parties")
    if not isinstance(parties, list) or not parties:
        runtime_state.set_status(
            _CREATION_RUNTIME_KIND,
            guild.id,
            operation_id,
            "invalid",
        )
        return False

    for party_index, raw_item in enumerate(parties):
        if not isinstance(raw_item, dict):
            return False
        item = dict(raw_item)
        content = str(item.get("content") or "")
        lines = content.splitlines()
        if not lines or not lines[0].startswith("Party "):
            return False
        message_id = int(item.get("starter_message_id") or 0) or None
        try:
            party_message = await _find_pending_party_message(
                destination_channel,
                operation_id,
                party_index,
                message_id,
            )
            if party_message is None:
                marker = _creation_marker(operation_id, party_index)
                checkpointed_content = _checkpointed_party_content(content, marker)
                kwargs = {"nonce": message_checkpoints.stable_nonce(marker)}
                if send_message is not None:
                    party_message = await send_message(checkpointed_content, **kwargs)
                else:
                    party_message = await destination_channel.send(checkpointed_content, **kwargs)
            item["starter_message_id"] = int(party_message.id)
            parties[party_index] = item
            payload["parties"] = parties
            record = _save_creation_record(
                guild.id,
                operation_id,
                payload,
                status="message_ready",
            )
            await _clean_party_creation_checkpoint(
                party_message,
                operation_id,
                party_index,
                desired_content=content,
            )

            thread_id = int(item.get("thread_id") or 0) or None
            thread = await _resolve_party_thread(guild, party_message, thread_id)
            if thread is None:
                thread = await party_message.create_thread(
                    name=str(item.get("thread_name") or f"{lines[0]} thread")[:100],
                    auto_archive_duration=60,
                    slowmode_delay=10,
                )
            item["thread_id"] = int(thread.id)
            parties[party_index] = item
            payload["parties"] = parties
            _save_creation_record(
                guild.id,
                operation_id,
                payload,
                status="party_ready",
            )
            _persist_party_message(
                party_message,
                content,
                thread_id=int(thread.id),
                source_message_id=int(payload.get("source_message_id") or 0) or None,
            )
        except (discord.Forbidden, discord.HTTPException, AttributeError, OSError):
            LOGGER.exception(
                "Composition creation %s stopped at party %s in guild %s",
                operation_id,
                party_index,
                guild.id,
            )
            return False

    payload[_MESSAGE_CHECKPOINT_CLEANUP_FIELD] = True
    _save_creation_record(
        guild.id,
        operation_id,
        payload,
        status="completed",
    )
    return True


async def _clean_completed_composition_creation(
    guild,
    record: runtime_state.RuntimeRecord,
) -> bool:
    """Remove checkpoints emitted by older releases, then record the migration."""

    payload = dict(record.payload)
    if payload.get(_MESSAGE_CHECKPOINT_CLEANUP_FIELD):
        return True
    try:
        destination_channel_id = int(payload.get("destination_channel_id") or 0)
    except (TypeError, ValueError):
        return False
    if not destination_channel_id:
        return False
    try:
        destination_channel = await _fetch_guild_channel(guild, destination_channel_id)
    except discord.NotFound:
        payload[_MESSAGE_CHECKPOINT_CLEANUP_FIELD] = True
        _save_creation_record(
            guild.id,
            str(record.external_id),
            payload,
            status="completed",
        )
        return True

    parties = payload.get("parties")
    if not isinstance(parties, list):
        return False
    for party_index, raw_item in enumerate(parties):
        if not isinstance(raw_item, dict):
            return False
        try:
            message_id = int(raw_item.get("starter_message_id") or 0) or None
        except (TypeError, ValueError):
            message_id = None
        party_message = await _find_pending_party_message(
            destination_channel,
            str(record.external_id),
            party_index,
            message_id,
        )
        if party_message is None:
            continue
        await _clean_party_creation_checkpoint(
            party_message,
            str(record.external_id),
            party_index,
        )

    payload[_MESSAGE_CHECKPOINT_CLEANUP_FIELD] = True
    _save_creation_record(
        guild.id,
        str(record.external_id),
        payload,
        status="completed",
    )
    return True


def create_composition_command(bot: commands.Bot) -> commands.Command:
    async def create_comp(
        context: commands.Context,
        comp_message_id: int,
        source_channel_id: Optional[int] = None,
    ) -> None:
        if context.guild is None or not isinstance(context.author, discord.Member):
            return

        caller_roles = guild_settings.get_caller_roles(context.guild.id)
        caller_role_ids = guild_settings.get_caller_role_ids(context.guild.id)
        if not has_caller_access(context.author, caller_roles, caller_role_ids):
            await context.send("You don't have permission to use this command.", delete_after=10)
            return

        if source_channel_id is None:
            await context.send("You must provide a **Channel ID** as a second parameter.")
            return

        source_channel = bot.get_channel(source_channel_id)
        if not isinstance(source_channel, discord.abc.Messageable):
            await context.send(
                "Could not find source channel. Make sure that **Channel ID** is correct."
            )
            return
        source_guild = getattr(source_channel, "guild", None)
        if source_guild is None or source_guild.id != context.guild.id:
            await context.send("The source channel must belong to this server.")
            return
        permissions_for = getattr(source_channel, "permissions_for", None)
        member_permissions = permissions_for(context.author) if permissions_for else None
        if member_permissions is None or not (
            member_permissions.view_channel and member_permissions.read_message_history
        ):
            await context.send("You do not have permission to view the source channel.")
            return

        try:
            source_message = await source_channel.fetch_message(comp_message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
            await context.send("Could not fetch comp message. Check the message and channel IDs.")
            return

        parties = [party.strip() for party in source_message.content.split("\n\n") if party.strip()]
        if not parties or any(not party.splitlines()[0].startswith("Party ") for party in parties):
            await context.send("Every composition section must start with **Party**.")
            return
        if len(parties) > MAX_PARTIES_PER_COMMAND:
            await context.send(
                f"A composition can contain at most {MAX_PARTIES_PER_COMMAND} parties."
            )
            return
        if any(len(party.splitlines()) > MAX_LINES_PER_PARTY for party in parties):
            await context.send(f"Each party can contain at most {MAX_LINES_PER_PARTY} lines.")
            return

        operation_id = uuid4().hex
        creation_payload = {
            "destination_channel_id": int(context.channel.id),
            "source_channel_id": int(source_channel_id),
            "source_message_id": int(source_message.id),
            "actor_id": int(context.author.id),
            "parties": [
                {
                    "content": party,
                    "thread_name": f"{party.splitlines()[0]} thread"[:100],
                }
                for party in parties
            ],
        }
        try:
            record = _save_creation_record(
                context.guild.id,
                operation_id,
                creation_payload,
                status="pending",
            )
        except Exception:
            LOGGER.exception(
                "Could not persist composition creation in guild %s",
                context.guild.id,
            )
            await context.send("Local storage is unavailable, so the composition was not created.")
            return

        try:
            await context.message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass

        completed = await _resume_composition_creation(
            bot,
            context.guild,
            record,
            destination_channel=context.channel,
            send_message=context.send,
        )
        if not completed:
            await context.send(
                "Composition creation was interrupted. Saved progress will be retried automatically."
            )

    return commands.Command(create_comp, name="create-comp")


def is_admin(member: discord.Member) -> bool:
    guild_permissions = getattr(member, "guild_permissions", None)
    if guild_permissions is not None:
        return bool(guild_permissions.administrator)
    return any(role.permissions.administrator for role in member.roles)


def has_caller_role(
    member: discord.Member,
    caller_roles: list[str],
    caller_role_ids: Optional[list[int]] = None,
) -> bool:
    return member_has_safe_privileged_role(
        member,
        role_ids=caller_role_ids,
        role_names=caller_roles,
    )


def has_caller_access(
    member: discord.Member,
    caller_roles: list[str],
    caller_role_ids: Optional[list[int]] = None,
) -> bool:
    return is_admin(member) or has_caller_role(
        member,
        caller_roles,
        caller_role_ids,
    )


def is_party_thread(channel: discord.abc.Messageable) -> bool:
    return (
        isinstance(channel, discord.Thread)
        and channel.name.startswith("Party ")
        and channel.name.endswith(" thread")
    )


def _unwrap_thread_starter_message(message):
    """Resolve Discord's empty type-21 wrapper to its parent-channel message."""

    if message is None:
        return None
    reference = getattr(message, "reference", None)
    referenced_message = getattr(message, "referenced_message", None) or getattr(
        reference,
        "resolved",
        None,
    )
    message_type = getattr(message, "type", None)
    same_message_reference = referenced_message is not None and int(
        getattr(referenced_message, "id", 0) or 0
    ) == int(getattr(message, "id", 0) or 0)
    if referenced_message is not None and (
        message_type == discord.MessageType.thread_starter_message
        or (not str(getattr(message, "content", "") or "") and same_message_reference)
    ):
        return referenced_message
    return message


async def _resolve_thread_parent(channel: discord.Thread):
    parent = getattr(channel, "parent", None)
    if parent is not None:
        return parent
    guild = getattr(channel, "guild", None)
    parent_id = int(getattr(channel, "parent_id", 0) or 0)
    if guild is None or not parent_id:
        return None
    parent = guild.get_channel(parent_id)
    if parent is not None:
        return parent
    try:
        return await guild.fetch_channel(parent_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
        return None


async def get_starter_message(
    channel: discord.Thread,
    *,
    message_id: Optional[int] = None,
    refresh: bool = False,
):
    """Return the editable parent message that owns a public party thread."""

    cached = _unwrap_thread_starter_message(getattr(channel, "starter_message", None))
    starter_id = int(message_id or getattr(channel, "id", 0) or getattr(cached, "id", 0) or 0)
    if (
        not refresh
        and cached is not None
        and int(getattr(cached, "id", 0) or 0) == starter_id
        and str(getattr(cached, "content", "") or "")
    ):
        return cached

    if starter_id:
        parent = await _resolve_thread_parent(channel)
        fetch_parent_message = getattr(parent, "fetch_message", None)
        if callable(fetch_parent_message):
            try:
                parent_message = await fetch_parent_message(starter_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
                parent_message = None
            if parent_message is not None:
                return _unwrap_thread_starter_message(parent_message)

        fetch_thread_message = getattr(channel, "fetch_message", None)
        if callable(fetch_thread_message):
            try:
                wrapper = await fetch_thread_message(starter_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
                wrapper = None
            resolved = _unwrap_thread_starter_message(wrapper)
            if resolved is not None:
                return resolved

    if cached is not None and int(getattr(cached, "id", 0) or 0) == starter_id:
        return cached
    return None


def parse_roles(comp_text: str) -> list:
    return comp_text.split("\n") if comp_text else []


def find_role_index_by_number(roles: list, number: int):
    for idx, role in enumerate(roles):
        if role.startswith(f"{number}. "):
            return idx
    return None


def find_first_mention(role_line: str):
    mention = _USER_MENTION_PATTERN.search(role_line)
    return mention.group(0) if mention else None


def _mention_user_id(mention: str) -> Optional[int]:
    match = _USER_MENTION_PATTERN.fullmatch(mention)
    return int(match.group(1)) if match else None


def _member_user_id(member) -> Optional[int]:
    member_id = int(getattr(member, "id", 0) or 0)
    if member_id:
        return member_id
    return _mention_user_id(str(getattr(member, "mention", "") or ""))


def find_role_index_by_member_id(roles: list, member_id: int) -> Optional[int]:
    for idx, role in enumerate(roles):
        mention = find_first_mention(str(role))
        if mention is not None and _mention_user_id(mention) == int(member_id):
            return idx
    return None


def _assigned_member_ids(roles: list) -> list[int]:
    """Return each Discord user mentioned in the rendered party once."""

    member_ids: list[int] = []
    for role in roles:
        mention = find_first_mention(str(role))
        member_id = _mention_user_id(mention) if mention is not None else None
        if member_id is not None and member_id not in member_ids:
            member_ids.append(member_id)
    return member_ids


async def update_comp_text(original_comp_text, roles):
    updated_content = "\n".join(roles)
    _persist_party_message(original_comp_text, updated_content)
    await original_comp_text.edit(
        content=updated_content,
        allowed_mentions=allowed_user_mentions(_assigned_member_ids(roles)),
    )


async def _fetch_guild_channel(guild, channel_id: int):
    """Resolve a guild channel without treating a cache miss as deletion."""

    get_thread = getattr(guild, "get_thread", None)
    if get_thread is not None:
        thread = get_thread(channel_id)
        if thread is not None:
            return thread
    channel = guild.get_channel(channel_id)
    if channel is not None:
        return channel
    return await guild.fetch_channel(channel_id)


def _is_valid_party_starter(starter, bot_user_id: int) -> bool:
    if not _is_bot_authored_starter(starter, bot_user_id):
        return False
    lines = str(getattr(starter, "content", "") or "").splitlines()
    return bool(lines and lines[0].startswith("Party "))


def _is_bot_authored_starter(starter, bot_user_id: int) -> bool:
    return (
        starter is not None
        and int(getattr(getattr(starter, "author", None), "id", 0) or 0) == bot_user_id
    )


async def _reconcile_persisted_composition(
    guild,
    record: runtime_state.RuntimeRecord,
    bot_user_id: int,
) -> None:
    thread_id_raw = record.payload.get("thread_id") or record.payload.get("channel_id")
    starter_id_raw = record.payload.get("starter_message_id") or record.external_id
    if (
        isinstance(thread_id_raw, bool)
        or not isinstance(thread_id_raw, (int, str))
        or isinstance(starter_id_raw, bool)
        or not isinstance(starter_id_raw, (int, str))
    ):
        LOGGER.warning(
            "Composition record %s in guild %s has invalid Discord IDs",
            record.external_id,
            guild.id,
        )
        return
    try:
        thread_id = int(thread_id_raw)
        starter_id = int(starter_id_raw)
    except (TypeError, ValueError):
        LOGGER.warning(
            "Composition record %s in guild %s has invalid Discord IDs",
            record.external_id,
            guild.id,
        )
        return

    try:
        thread = await _fetch_guild_channel(guild, thread_id)
    except discord.NotFound:
        runtime_state.set_status(
            _RUNTIME_KIND,
            guild.id,
            record.external_id,
            "missing",
        )
        return
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        LOGGER.warning(
            "Could not resolve composition thread %s in guild %s",
            thread_id,
            guild.id,
        )
        return

    if not isinstance(thread, discord.Thread) or not is_party_thread(thread):
        LOGGER.warning(
            "Refusing to reconcile composition %s: Discord resource %s is not its party thread",
            record.external_id,
            thread_id,
        )
        return

    starter = await get_starter_message(
        thread,
        message_id=starter_id,
        refresh=True,
    )
    if starter is None or int(getattr(starter, "id", 0) or 0) != starter_id:
        LOGGER.warning(
            "Could not fetch composition starter %s in guild %s",
            starter_id,
            guild.id,
        )
        return

    if not _is_bot_authored_starter(starter, bot_user_id):
        LOGGER.warning(
            "Refusing to edit composition starter %s because ownership changed",
            starter_id,
        )
        return

    try:
        await message_checkpoints.clean_message_checkpoint_prefixes(
            starter,
            (f"{_CREATION_MARKER_PREFIX}:",),
        )
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        LOGGER.warning(
            "Composition starter %s still has legacy checkpoint metadata",
            starter_id,
        )

    desired_content = str(record.payload.get("content") or "")
    desired_lines = desired_content.splitlines()
    if not desired_lines or not desired_lines[0].startswith("Party "):
        LOGGER.warning(
            "Refusing to edit composition starter %s because its SQLite payload is invalid",
            starter_id,
        )
        return
    current_content = message_checkpoints.strip_hidden_checkpoints(
        getattr(starter, "content", ""),
    )
    if desired_content and desired_content != current_content:
        try:
            await starter.edit(
                content=desired_content,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            LOGGER.warning(
                "Could not restore composition party %s in guild %s",
                starter_id,
                guild.id,
            )
            return

    if record.status == "missing":
        runtime_state.set_status(
            _RUNTIME_KIND,
            guild.id,
            record.external_id,
            "active",
        )


async def reconcile_compositions(bot: discord.Client) -> None:
    """Restore SQLite-tracked parties, then import untracked cached legacy ones."""

    bot_user_id = int(getattr(getattr(bot, "user", None), "id", 0) or 0)
    if not bot_user_id:
        return
    for guild in getattr(bot, "guilds", ()):
        creation_statuses = {"pending", "message_ready", "party_ready"}
        for creation_record in runtime_state.list_records(
            _CREATION_RUNTIME_KIND,
            guild_id=guild.id,
            statuses=tuple(creation_statuses),
        ):
            if getattr(creation_record, "status", None) not in creation_statuses:
                continue
            try:
                await _resume_composition_creation(bot, guild, creation_record)
            except Exception:
                LOGGER.exception(
                    "Could not resume composition creation %s in guild %s",
                    creation_record.external_id,
                    guild.id,
                )
        for creation_record in runtime_state.list_records(
            _CREATION_RUNTIME_KIND,
            guild_id=guild.id,
            statuses=("completed",),
        ):
            if getattr(
                creation_record, "status", None
            ) != "completed" or creation_record.payload.get(_MESSAGE_CHECKPOINT_CLEANUP_FIELD):
                continue
            try:
                await _clean_completed_composition_creation(guild, creation_record)
            except (
                discord.Forbidden,
                discord.HTTPException,
                AttributeError,
                OSError,
            ):
                LOGGER.warning(
                    "Composition checkpoints still need cleanup for %s in guild %s",
                    creation_record.external_id,
                    guild.id,
                )
            except Exception:
                LOGGER.exception(
                    "Composition checkpoint cleanup failed for %s in guild %s",
                    creation_record.external_id,
                    guild.id,
                )
        tracked_starter_ids: set[int] = set()
        for record in runtime_state.list_records(
            _RUNTIME_KIND,
            guild_id=guild.id,
            statuses=("active", "missing"),
        ):
            try:
                tracked_starter_ids.add(int(record.external_id))
            except ValueError:
                pass
            await _reconcile_persisted_composition(guild, record, bot_user_id)

        # Cached active threads are only a discovery path for compositions created
        # before runtime persistence existed. Never recreate a missing thread.
        for thread in getattr(guild, "threads", ()):
            if not is_party_thread(thread):
                continue
            starter = await get_starter_message(thread)
            if not _is_valid_party_starter(starter, bot_user_id):
                continue
            if int(starter.id) in tracked_starter_ids:
                continue
            try:
                await message_checkpoints.clean_message_checkpoint_prefixes(
                    starter,
                    (f"{_CREATION_MARKER_PREFIX}:",),
                )
            except (discord.Forbidden, discord.HTTPException, AttributeError):
                LOGGER.warning(
                    "Legacy composition starter %s still has checkpoint metadata",
                    starter.id,
                )
            clean_content = message_checkpoints.strip_hidden_checkpoints(
                getattr(starter, "content", ""),
            )
            _persist_party_message(
                starter,
                clean_content,
                thread_id=thread.id,
            )


async def officer_forced_signout(
    message,
    roles,
    original_comp_text,
    role_number: int,
    caller_roles: Optional[list[str]] = None,
    caller_role_ids: Optional[list[int]] = None,
):
    member = message.author
    if not has_caller_access(member, caller_roles or [], caller_role_ids):
        return
    idx = find_role_index_by_number(roles, role_number)
    if idx is None:
        return
    mention = find_first_mention(roles[idx])
    if not mention:
        return
    role_name = roles[idx].split(f"{role_number}. ")[1].split(f" {mention}")[0].strip()
    roles[idx] = roles[idx].replace(f" {mention}", "")
    await update_comp_text(original_comp_text, roles)
    await message.reply(
        f"{mention} was signed out from **{role_name}**",
        allowed_mentions=allowed_user_mentions([_mention_user_id(mention)]),
    )


async def sign_up_user(message, roles, original_comp_text, role_number: int, member=None):
    idx = find_role_index_by_number(roles, role_number)
    if idx is None:
        await message.reply(
            "That role number does not exist in this party.",
            allowed_mentions=allowed_user_mentions([]),
        )
        return
    if member is None:
        member = message.author

    member_id = _member_user_id(member)
    author_id = _member_user_id(message.author)
    if member_id is None:
        return
    existing_idx = find_role_index_by_member_id(roles, member_id)
    if existing_idx is not None:
        is_self_signup = author_id == member_id
        if existing_idx == idx:
            rejection = (
                "You are already signed up for this role."
                if is_self_signup
                else "That member is already signed up for this role."
            )
        else:
            rejection = (
                "You are already signed up for another role. Sign out first with `-`."
                if is_self_signup
                else "That member is already signed up for another role."
            )
        await message.reply(
            rejection,
            allowed_mentions=allowed_user_mentions([]),
        )
        return

    current_mention = find_first_mention(roles[idx])
    if current_mention:
        await message.reply(
            "This role is already taken.",
            allowed_mentions=allowed_user_mentions([]),
        )
        return

    role_name = roles[idx].split(f"{role_number}. ")[1].split(f" {member.mention}")[0].strip()
    roles[idx] = roles[idx] + f" {member.mention}"
    await update_comp_text(original_comp_text, roles)
    await message.reply(
        f"{member.mention} was signed up as **{role_name}**",
        allowed_mentions=allowed_user_mentions([_mention_user_id(str(member.mention))]),
    )


async def sign_out_self(message, roles, original_comp_text):
    member_id = _member_user_id(message.author)
    if member_id is None:
        return
    removed_role_names: list[str] = []
    for idx, role in enumerate(roles):
        mention = find_first_mention(role)
        if mention is None or _mention_user_id(mention) != member_id:
            continue
        role_parts = role.split(". ", 1)
        if len(role_parts) != 2:
            continue
        role_name = role_parts[1].replace(mention, "", 1).strip()
        roles[idx] = role.replace(mention, "", 1).rstrip()
        removed_role_names.append(role_name)

    if not removed_role_names:
        await message.reply(
            "You are not signed up for a role in this party.",
            allowed_mentions=allowed_user_mentions([]),
        )
        return

    await update_comp_text(original_comp_text, roles)
    formatted_roles = ", ".join(f"**{role_name}**" for role_name in removed_role_names)
    await message.reply(
        f"{message.author.mention} was signed out from {formatted_roles}",
        allowed_mentions=allowed_user_mentions([member_id]),
    )


async def on_message_in_thread(message):
    if message.author.bot:
        return
    if not is_party_thread(message.channel):
        return

    user_text = str(message.content or "").strip()
    original_comp_text = await get_starter_message(message.channel)
    if original_comp_text is None:
        return
    bot_member = getattr(message.guild, "me", None) if message.guild else None
    if (
        bot_member is None
        or getattr(getattr(original_comp_text, "author", None), "id", None) != bot_member.id
        or not (original_comp_text.content or "").splitlines()
        or not (original_comp_text.content or "").splitlines()[0].startswith("Party ")
    ):
        return

    # Discord messages are an optimistic shared document. Serializing mutations per
    # starter message prevents two simultaneous sign-ups from claiming the same slot.
    async with _message_locks.hold(int(original_comp_text.id)):
        original_comp_text = await get_starter_message(
            message.channel,
            message_id=int(original_comp_text.id),
            refresh=True,
        )
        if original_comp_text is None:
            return
        if (
            getattr(getattr(original_comp_text, "author", None), "id", None) != bot_member.id
            or not (original_comp_text.content or "").splitlines()
            or not (original_comp_text.content or "").splitlines()[0].startswith("Party ")
        ):
            return

        roles = parse_roles(original_comp_text.content)
        caller_roles = guild_settings.get_caller_roles(message.guild.id) if message.guild else []
        caller_role_ids = (
            guild_settings.get_caller_role_ids(message.guild.id) if message.guild else []
        )

        forced_signout_match = re.fullmatch(r"-([0-9]+)", user_text)
        if forced_signout_match:
            await officer_forced_signout(
                message,
                roles,
                original_comp_text,
                int(forced_signout_match.group(1)),
                caller_roles,
                caller_role_ids,
            )
            return

        signup_match = re.fullmatch(r"[0-9]+", user_text)
        if signup_match:
            await sign_up_user(message, roles, original_comp_text, int(signup_match.group(0)))
            return

        if has_caller_access(message.author, caller_roles, caller_role_ids):
            match = re.fullmatch(r"(<@!?[0-9]+>)\s+([0-9]+)", user_text)
            if match:
                mention = match.group(1)
                role_number = int(match.group(2))
                user_id = int(mention[2:-1].lstrip("!"))
                member = next(
                    (
                        mentioned_member
                        for mentioned_member in getattr(message, "mentions", ())
                        if int(getattr(mentioned_member, "id", 0) or 0) == user_id
                    ),
                    None,
                ) or message.guild.get_member(user_id)
                if member:
                    await sign_up_user(message, roles, original_comp_text, role_number, member)
                    return
                await message.reply(
                    "Could not find that server member.",
                    allowed_mentions=allowed_user_mentions([]),
                )
                return

        if user_text == "-":
            await sign_out_self(message, roles, original_comp_text)
