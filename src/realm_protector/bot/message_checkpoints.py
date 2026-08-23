"""Transient, invisible checkpoints for crash-safe Discord message workflows.

SQLite stores the authoritative workflow state and Discord resource IDs. A
short zero-width token closes the remaining crash window between a successful
Discord send and the following local commit without exposing internal IDs to
server members. The token is removed once the Discord resource ID is durable.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from typing import Optional

import discord

_HIDDEN_PREFIX = "\u2063\u2060\u2063"
_HIDDEN_SUFFIX = "\u2063\u2060\u2060"
_HIDDEN_ZERO = "\u200b"
_HIDDEN_ONE = "\u200c"
_NONCE_MASK = (1 << 63) - 1
_NO_MENTIONS = discord.AllowedMentions.none()


def stable_nonce(marker: str) -> int:
    """Return a deterministic Discord-safe integer for one marker."""

    digest = hashlib.blake2b(
        f"realm-protector:message-checkpoint:{marker}".encode("utf-8"),
        digest_size=8,
    ).digest()
    return int.from_bytes(digest, "big") & _NONCE_MASK


def hidden_checkpoint(
    marker: str,
    *,
    nonce: Optional[int] = None,
    bit_count: int = 63,
) -> str:
    """Encode a checkpoint as zero-width Unicode retained by Discord."""

    if not 1 <= int(bit_count) <= 63:
        raise ValueError("bit_count must be between 1 and 63")
    mask = (1 << int(bit_count)) - 1
    resolved_nonce = int(nonce if nonce is not None else stable_nonce(marker)) & mask
    encoded = format(resolved_nonce, f"0{int(bit_count)}b")
    bits = "".join(_HIDDEN_ONE if bit == "1" else _HIDDEN_ZERO for bit in encoded)
    return f"{_HIDDEN_PREFIX}{bits}{_HIDDEN_SUFFIX}"


def content_with_checkpoint(
    content: Optional[str],
    marker: str,
    *,
    nonce: Optional[int] = None,
    max_length: int = 2000,
    bit_count: int = 63,
) -> str:
    """Append an invisible checkpoint while respecting Discord's content limit."""

    checkpoint = hidden_checkpoint(marker, nonce=nonce, bit_count=bit_count)
    if max_length < len(checkpoint):
        raise ValueError("max_length is too small for checkpoint metadata")
    visible_content = str(content or "")[: max_length - len(checkpoint)]
    return f"{visible_content}{checkpoint}"


def strip_checkpoint(
    content: object,
    marker: str,
    *,
    nonce: Optional[int] = None,
    bit_count: int = 63,
) -> str:
    """Remove this workflow's invisible token without changing visible text."""

    return str(content or "").replace(
        hidden_checkpoint(marker, nonce=nonce, bit_count=bit_count),
        "",
    )


def strip_hidden_checkpoints(
    content: object,
    *,
    bit_counts: Iterable[int] = (63,),
) -> str:
    """Remove well-formed transient tokens when their exact keys are unavailable."""

    allowed_counts = frozenset(int(bit_count) for bit_count in bit_counts)
    if not allowed_counts or any(bit_count < 1 or bit_count > 63 for bit_count in allowed_counts):
        raise ValueError("bit_counts must contain values between 1 and 63")

    value = str(content or "")
    retained: list[str] = []
    offset = 0
    while True:
        prefix_index = value.find(_HIDDEN_PREFIX, offset)
        if prefix_index < 0:
            retained.append(value[offset:])
            break
        retained.append(value[offset:prefix_index])
        body_start = prefix_index + len(_HIDDEN_PREFIX)
        suffix_index = value.find(_HIDDEN_SUFFIX, body_start)
        if suffix_index < 0:
            retained.append(value[prefix_index:])
            break
        body = value[body_start:suffix_index]
        if len(body) in allowed_counts and all(bit in {_HIDDEN_ZERO, _HIDDEN_ONE} for bit in body):
            offset = suffix_index + len(_HIDDEN_SUFFIX)
            continue
        retained.append(value[prefix_index:body_start])
        offset = body_start
    return "".join(retained)


def message_has_checkpoint(
    message: object,
    marker: str,
    *,
    nonce: Optional[int] = None,
    bit_count: int = 63,
) -> bool:
    """Recognize new hidden metadata and every legacy visible marker form."""

    if not 1 <= int(bit_count) <= 63:
        raise ValueError("bit_count must be between 1 and 63")
    mask = (1 << int(bit_count)) - 1
    resolved_nonce = int(nonce if nonce is not None else stable_nonce(marker)) & mask
    content = str(getattr(message, "content", "") or "")
    if (
        marker in content
        or hidden_checkpoint(
            marker,
            nonce=resolved_nonce,
            bit_count=bit_count,
        )
        in content
    ):
        return True
    if str(getattr(message, "nonce", "")) == str(resolved_nonce):
        return True
    for embed in getattr(message, "embeds", ()) or ():
        footer = getattr(embed, "footer", None)
        if str(getattr(footer, "text", "") or "") == marker:
            return True
    return False


def _embed_is_empty(embed: discord.Embed) -> bool:
    data = embed.to_dict()
    data.pop("type", None)
    if not data.get("flags"):
        data.pop("flags", None)
    return not data


def _clean_checkpoint_embeds(
    message: object,
    footer_matches: Callable[[str], bool],
) -> tuple[list[discord.Embed], bool]:
    retained_embeds: list[discord.Embed] = []
    embeds_changed = False
    for original in getattr(message, "embeds", ()) or ():
        embed = original.copy()
        footer = getattr(embed, "footer", None)
        if footer_matches(str(getattr(footer, "text", "") or "")):
            embed.remove_footer()
            embeds_changed = True
            if _embed_is_empty(embed):
                continue
        retained_embeds.append(embed)
    return retained_embeds, embeds_changed


async def _apply_checkpoint_cleanup(
    message: discord.Message,
    *,
    original_content: str,
    cleaned_content: str,
    retained_embeds: list[discord.Embed],
    embeds_changed: bool,
) -> bool:
    content_changed = cleaned_content != original_content
    if not content_changed and not embeds_changed:
        return False
    if content_changed and embeds_changed:
        await message.edit(
            content=cleaned_content or None,
            embeds=retained_embeds,
            allowed_mentions=_NO_MENTIONS,
        )
    elif content_changed:
        await message.edit(
            content=cleaned_content or None,
            allowed_mentions=_NO_MENTIONS,
        )
    else:
        await message.edit(embeds=retained_embeds)
    return True


async def clean_message_checkpoint(
    message: discord.Message,
    marker: str,
    *,
    nonce: Optional[int] = None,
    bit_count: int = 63,
) -> bool:
    """Remove hidden/legacy metadata while preserving visible message data."""

    original_content = str(getattr(message, "content", "") or "")
    cleaned_content = strip_checkpoint(
        original_content,
        marker,
        nonce=nonce,
        bit_count=bit_count,
    )
    retained_embeds, embeds_changed = _clean_checkpoint_embeds(
        message,
        lambda footer_text: footer_text == marker,
    )
    return await _apply_checkpoint_cleanup(
        message,
        original_content=original_content,
        cleaned_content=cleaned_content,
        retained_embeds=retained_embeds,
        embeds_changed=embeds_changed,
    )


async def clean_message_checkpoint_prefixes(
    message: discord.Message,
    marker_prefixes: Iterable[str],
    *,
    bit_counts: Iterable[int] = (63,),
) -> bool:
    """Clean legacy footer families and transient tokens from a known bot artifact."""

    prefixes = tuple(str(prefix) for prefix in marker_prefixes if str(prefix))
    if not prefixes:
        raise ValueError("marker_prefixes must not be empty")
    original_content = str(getattr(message, "content", "") or "")
    cleaned_content = strip_hidden_checkpoints(
        original_content,
        bit_counts=bit_counts,
    )
    retained_embeds, embeds_changed = _clean_checkpoint_embeds(
        message,
        lambda footer_text: footer_text.startswith(prefixes),
    )
    return await _apply_checkpoint_cleanup(
        message,
        original_content=original_content,
        cleaned_content=cleaned_content,
        retained_embeds=retained_embeds,
        embeds_changed=embeds_changed,
    )


__all__ = [
    "clean_message_checkpoint",
    "clean_message_checkpoint_prefixes",
    "content_with_checkpoint",
    "hidden_checkpoint",
    "message_has_checkpoint",
    "stable_nonce",
    "strip_checkpoint",
    "strip_hidden_checkpoints",
]
