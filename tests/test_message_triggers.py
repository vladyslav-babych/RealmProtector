import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from src.realm_protector.bot import message_triggers


class HousriMessageTriggerTests(unittest.IsolatedAsyncioTestCase):
    def test_matches_only_a_case_insensitive_complete_word(self) -> None:
        matching = (
            "housri",
            "HOUSRI",
            "hello Housri!",
            "**hOuSrI**",
            "housri's party",
        )
        non_matching = (
            "",
            "house",
            "housrish",
            "prehousri",
            "housri_1",
        )

        for content in matching:
            with self.subTest(content=content):
                self.assertTrue(message_triggers.contains_housri(content))
        for content in non_matching:
            with self.subTest(content=content):
                self.assertFalse(message_triggers.contains_housri(content))

    async def test_replies_with_gif_without_ping_for_a_server_message(self) -> None:
        message = SimpleNamespace(
            guild=SimpleNamespace(id=10),
            author=SimpleNamespace(bot=False),
            content="That was HOUSRI!",
            id=20,
            webhook_id=None,
            reply=AsyncMock(),
        )
        gif_path = Path("resources/gif/8x4qbf.gif")
        attached_file = object()

        with patch.object(message_triggers.discord, "File", return_value=attached_file) as file:
            await message_triggers.post_housri_gif(message, gif_path)

        file.assert_called_once_with(gif_path, filename="8x4qbf.gif")
        message.reply.assert_awaited_once_with(
            file=attached_file,
            mention_author=False,
            allowed_mentions=unittest.mock.ANY,
        )
        allowed_mentions = message.reply.await_args.kwargs["allowed_mentions"]
        self.assertIsInstance(allowed_mentions, discord.AllowedMentions)
        self.assertFalse(allowed_mentions.everyone)
        self.assertFalse(allowed_mentions.users)
        self.assertFalse(allowed_mentions.roles)
        self.assertFalse(allowed_mentions.replied_user)

    async def test_ignores_direct_messages_bot_messages_and_substrings(self) -> None:
        cases = (
            SimpleNamespace(
                guild=None,
                author=SimpleNamespace(bot=False),
                content="housri",
            ),
            SimpleNamespace(
                guild=SimpleNamespace(id=10),
                author=SimpleNamespace(bot=True),
                content="housri",
                webhook_id=None,
            ),
            SimpleNamespace(
                guild=SimpleNamespace(id=10),
                author=SimpleNamespace(bot=False),
                content="housri",
                webhook_id=30,
            ),
            SimpleNamespace(
                guild=SimpleNamespace(id=10),
                author=SimpleNamespace(bot=False),
                content="housrish",
                webhook_id=None,
            ),
        )

        for message in cases:
            message.reply = AsyncMock()
            with self.subTest(message=message):
                await message_triggers.post_housri_gif(message, Path("response.gif"))
                message.reply.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
