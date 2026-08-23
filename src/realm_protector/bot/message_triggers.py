"""Small, server-wide message reactions owned by the Discord presentation layer."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import discord

LOGGER = logging.getLogger(__name__)

# ``\b`` would also be correct for ASCII text, but explicit word-character
# lookarounds make the intended no-substring behavior easier to see and test.
_HOUSRI_PATTERN = re.compile(r"(?<!\w)housri(?!\w)", flags=re.IGNORECASE)


def contains_housri(content: str) -> bool:
    """Return whether *content* contains ``housri`` as a case-insensitive word."""

    return _HOUSRI_PATTERN.search(content) is not None


async def post_housri_gif(message: discord.Message, gif_path: Path) -> None:
    """Reply with the configured GIF to qualifying human-authored guild messages."""

    if message.guild is None or message.author.bot or message.webhook_id is not None:
        return
    if not contains_housri(message.content or ""):
        return

    try:
        await message.reply(
            file=discord.File(gif_path, filename=gif_path.name),
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except OSError:
        LOGGER.exception("Housri response GIF could not be opened: %s", gif_path)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        LOGGER.warning(
            "Could not post the Housri response GIF for guild %s message %s",
            message.guild.id,
            message.id,
            exc_info=True,
        )


__all__ = ["contains_housri", "post_housri_gif"]
