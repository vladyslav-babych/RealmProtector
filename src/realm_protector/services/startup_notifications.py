from __future__ import annotations

import logging
from pathlib import Path

DEFAULT_STARTUP_MESSAGE = "Bot has been restarted"
DISCORD_MESSAGE_LIMIT = 2_000


def load_startup_message(message_path: Path) -> str:
    """Load startup presentation copy from the configured text resource."""
    logger = logging.getLogger(__name__)
    try:
        message = message_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as error:
        logger.warning("Could not read startup message %s: %s", message_path, error)
        message = DEFAULT_STARTUP_MESSAGE

    if not message:
        message = DEFAULT_STARTUP_MESSAGE
    return message[:DISCORD_MESSAGE_LIMIT]
