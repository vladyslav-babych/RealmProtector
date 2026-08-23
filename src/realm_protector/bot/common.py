from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Optional

import discord

DISCORD_SAFE_MESSAGE_LIMIT = 1_900


def allowed_user_mentions(user_ids: Iterable[object]) -> discord.AllowedMentions:
    """Allow pings only for the explicit Discord user IDs in this message."""
    users: list[discord.Object] = []
    seen_ids: set[int] = set()
    for raw_user_id in user_ids:
        if isinstance(raw_user_id, bool):
            continue
        try:
            user_id = int(str(raw_user_id))
        except (TypeError, ValueError):
            continue
        if user_id <= 0 or user_id in seen_ids:
            continue
        seen_ids.add(user_id)
        users.append(discord.Object(id=user_id))

    return discord.AllowedMentions(
        users=users,
        roles=False,
        everyone=False,
        replied_user=False,
    )


def parse_csv_values(raw_value: str, *, deduplicate: bool = False) -> list[str]:
    """Parse a comma-separated Discord option while preserving user order."""
    values = [value.strip() for value in (raw_value or "").split(",") if value.strip()]
    if not deduplicate:
        return values
    return list(dict.fromkeys(values))


def chunk_lines(
    lines: Iterable[str],
    *,
    limit: int = DISCORD_SAFE_MESSAGE_LIMIT,
) -> list[str]:
    """Group lines into Discord-safe messages without discarding long content."""
    if limit <= 0:
        raise ValueError("limit must be positive")

    chunks: list[str] = []
    current: list[str] = []
    current_length = 0

    for original_line in lines:
        line = str(original_line)
        if len(line) > limit:
            if current:
                chunks.append("\n".join(current))
                current = []
                current_length = 0
            chunks.extend(line[index : index + limit] for index in range(0, len(line), limit))
            continue

        extra_length = len(line) + (1 if current else 0)
        if current and current_length + extra_length > limit:
            chunks.append("\n".join(current))
            current = [line]
            current_length = len(line)
            continue

        current.append(line)
        current_length += extra_length

    if current:
        chunks.append("\n".join(current))
    return chunks


async def send_followup_lines(
    interaction: discord.Interaction,
    lines: Iterable[str],
    *,
    limit: int = DISCORD_SAFE_MESSAGE_LIMIT,
    allowed_mentions: Optional[discord.AllowedMentions] = None,
) -> None:
    for chunk in chunk_lines(lines, limit=limit):
        kwargs: dict[str, Any] = {}
        if allowed_mentions is not None:
            kwargs["allowed_mentions"] = allowed_mentions
        await interaction.followup.send(chunk, **kwargs)


class InteractionMessageAdapter:
    """Expose the small Context-like surface required by legacy-free services."""

    def __init__(
        self,
        interaction: discord.Interaction,
        *,
        ephemeral: bool = False,
    ) -> None:
        self._interaction = interaction
        self._ephemeral = bool(ephemeral)
        self.guild = interaction.guild
        self.author = interaction.user

    async def send(self, content: Optional[str] = None, **kwargs: Any) -> None:
        if self._ephemeral:
            kwargs.setdefault("ephemeral", True)
        if not self._interaction.response.is_done():
            if content is None:
                await self._interaction.response.send_message(**kwargs)
            else:
                await self._interaction.response.send_message(content, **kwargs)
            return
        if content is None:
            await self._interaction.followup.send(**kwargs)
        else:
            await self._interaction.followup.send(content, **kwargs)
