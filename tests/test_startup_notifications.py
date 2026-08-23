import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.realm_protector.services.startup_notifications import (
    DEFAULT_STARTUP_MESSAGE,
    DISCORD_MESSAGE_LIMIT,
    load_startup_message,
)


class StartupNotificationTests(unittest.TestCase):
    def test_loads_trimmed_message_from_text_file(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            message_path = Path(temporary_directory) / "startup_notification.txt"
            message_path.write_text("\n  Deployed from the text file.  \n", encoding="utf-8")

            self.assertEqual("Deployed from the text file.", load_startup_message(message_path))

    def test_empty_or_missing_file_uses_safe_default(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            message_path = Path(temporary_directory) / "startup_notification.txt"
            message_path.write_text(" \n", encoding="utf-8")

            self.assertEqual(DEFAULT_STARTUP_MESSAGE, load_startup_message(message_path))
            self.assertEqual(
                DEFAULT_STARTUP_MESSAGE,
                load_startup_message(Path(temporary_directory) / "missing.txt"),
            )

    def test_message_is_limited_to_discord_content_limit(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            message_path = Path(temporary_directory) / "startup_notification.txt"
            message_path.write_text("x" * (DISCORD_MESSAGE_LIMIT + 100), encoding="utf-8")

            self.assertEqual("x" * DISCORD_MESSAGE_LIMIT, load_startup_message(message_path))


if __name__ == "__main__":
    unittest.main()
