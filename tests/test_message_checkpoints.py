import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from src.realm_protector.bot import message_checkpoints


class MessageCheckpointTests(unittest.IsolatedAsyncioTestCase):
    def test_hidden_checkpoint_is_stable_and_recognized_without_nonce(self) -> None:
        marker = "workflow:operation-one:message"
        content = message_checkpoints.content_with_checkpoint("Visible", marker)
        message = SimpleNamespace(content=content, nonce=None, embeds=[])

        self.assertTrue(content.startswith("Visible"))
        self.assertTrue(message_checkpoints.message_has_checkpoint(message, marker))
        self.assertEqual("Visible", message_checkpoints.strip_checkpoint(content, marker))

    async def test_cleanup_drops_marker_only_embed_and_preserves_real_embed(self) -> None:
        marker = "workflow:operation-one:message"
        real_embed = discord.Embed(title="Visible information")
        marker_embed = discord.Embed()
        marker_embed.set_footer(text=marker)
        message = SimpleNamespace(
            content=message_checkpoints.content_with_checkpoint("Visible", marker),
            embeds=[real_embed, marker_embed],
            edit=AsyncMock(),
        )

        changed = await message_checkpoints.clean_message_checkpoint(message, marker)

        self.assertTrue(changed)
        options = message.edit.await_args.kwargs
        self.assertEqual("Visible", options["content"])
        self.assertEqual(1, len(options["embeds"]))
        self.assertEqual(real_embed.to_dict(), options["embeds"][0].to_dict())

    async def test_cleanup_removes_footer_from_a_real_embed(self) -> None:
        marker = "workflow:operation-one:message"
        embed = discord.Embed(title="Visible information")
        embed.set_footer(text=marker)
        message = SimpleNamespace(content="", embeds=[embed], edit=AsyncMock())

        await message_checkpoints.clean_message_checkpoint(message, marker)

        cleaned = message.edit.await_args.kwargs["embeds"][0]
        self.assertEqual("Visible information", cleaned.title)
        self.assertIsNone(cleaned.footer.text)

    async def test_prefix_cleanup_handles_unknown_legacy_id_and_hidden_token(self) -> None:
        marker = "workflow:unknown-operation"
        embed = discord.Embed(title="Visible information")
        embed.set_footer(text=marker)
        message = SimpleNamespace(
            content=message_checkpoints.content_with_checkpoint("Visible", marker),
            embeds=[embed],
            edit=AsyncMock(),
        )

        changed = await message_checkpoints.clean_message_checkpoint_prefixes(
            message,
            ("workflow:",),
        )

        self.assertTrue(changed)
        options = message.edit.await_args.kwargs
        self.assertEqual("Visible", options["content"])
        self.assertEqual("Visible information", options["embeds"][0].title)
        self.assertIsNone(options["embeds"][0].footer.text)


if __name__ == "__main__":
    unittest.main()
