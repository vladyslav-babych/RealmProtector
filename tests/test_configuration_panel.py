import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from src.realm_protector.bot import configuration_panel, message_checkpoints


def _configuration(**updates):
    values = {
        "target_guild_name": "Kingsblood",
        "caller_role_names": [],
        "economy_manager_role_names": [],
        "member_role_name": "Member",
        "caller_role_ids": [],
        "economy_manager_role_ids": [],
        "member_role_id": None,
        "bot_updates_channel_id": None,
        "bot_configuration_channel_id": None,
        "bot_configuration_message_id": None,
    }
    values.update(updates)
    return SimpleNamespace(**values)


class ConfigurationPanelCheckpointTests(unittest.IsolatedAsyncioTestCase):
    def test_rendered_configuration_embed_has_no_internal_footer(self) -> None:
        guild = SimpleNamespace(id=30, roles=[], get_role=lambda _role_id: None)

        with (
            patch.object(
                configuration_panel.guild_settings,
                "get_configuration",
                return_value=_configuration(),
            ),
            patch.object(
                configuration_panel.credential_store,
                "get_credentials_info",
                return_value=None,
            ),
        ):
            embed = configuration_panel._build_bot_configuration_panel(guild)

        self.assertIsNone(embed.footer.text)

    async def test_persisted_message_id_is_authoritative_without_checkpoint(self) -> None:
        expected = SimpleNamespace(id=555, content="", embeds=[], nonce=None)
        channel = SimpleNamespace(
            fetch_message=AsyncMock(return_value=expected),
            history=MagicMock(),
        )

        resolved = await configuration_panel._find_publication_message(
            channel,
            configuration_panel._publication_marker(30),
            message_id=555,
        )

        self.assertIs(expected, resolved)
        channel.history.assert_not_called()

    async def test_history_recovery_ignores_another_authors_checkpoint(self) -> None:
        marker = configuration_panel._publication_marker(30)
        spoof = SimpleNamespace(
            author=SimpleNamespace(id=999),
            content=message_checkpoints.content_with_checkpoint(None, marker),
            embeds=[],
            nonce=None,
        )
        expected = SimpleNamespace(
            author=SimpleNamespace(id=100),
            content=message_checkpoints.content_with_checkpoint(None, marker),
            embeds=[],
            nonce=None,
        )

        async def history(*, limit):
            self.assertEqual(configuration_panel._FALLBACK_HISTORY_LIMIT, limit)
            for _ in range(125):
                yield SimpleNamespace(
                    author=SimpleNamespace(id=100),
                    content="unrelated",
                    embeds=[],
                    nonce=None,
                )
            yield spoof
            yield expected

        channel = SimpleNamespace(
            guild=SimpleNamespace(me=SimpleNamespace(id=100)),
            history=history,
        )

        resolved = await configuration_panel._find_publication_message(channel, marker)

        self.assertIs(expected, resolved)

    async def test_new_panel_checkpoint_is_removed_after_message_id_is_saved(self) -> None:
        events: list[str] = []
        marker = configuration_panel._publication_marker(30)
        final_embed = discord.Embed(title="Bot Configuration", description="Current settings")
        sent: dict[str, object] = {}

        class MutableMessage:
            id = 555

            def __init__(self, content: str, embed: discord.Embed) -> None:
                self.content = content
                self.embeds = [embed]
                self.nonce = None

            async def edit(self, **options):
                events.append("edit")
                if "content" in options:
                    self.content = str(options["content"] or "")
                if "embed" in options:
                    embed = options["embed"]
                    self.embeds = [embed] if embed is not None else []
                if "embeds" in options:
                    self.embeds = list(options["embeds"])
                return self

        async def empty_history(*, limit):
            self.assertEqual(configuration_panel._FALLBACK_HISTORY_LIMIT, limit)
            if False:
                yield None

        async def send(**options):
            events.append("send")
            sent.update(options)
            message = MutableMessage(str(options.get("content") or ""), options["embed"])
            sent["message"] = message
            return message

        channel = SimpleNamespace(id=10, history=empty_history, send=send)
        guild = SimpleNamespace(id=30)
        record = SimpleNamespace(
            external_id="panel",
            payload={"channel_id": 10, "marker": marker},
        )
        persist_calls: list[dict] = []

        def persist(_guild_id, channel_id, **options):
            events.append(options["status"])
            persist_calls.append(dict(options))
            payload = {"channel_id": channel_id, "marker": marker}
            if options.get("message_id"):
                payload["message_id"] = options["message_id"]
            return SimpleNamespace(external_id="panel", payload=payload, status=options["status"])

        with (
            patch.object(
                configuration_panel.guild_settings,
                "get_configuration",
                return_value=_configuration(),
            ),
            patch.object(
                configuration_panel.guild_settings,
                "set_bot_configuration_message",
            ),
            patch.object(
                configuration_panel.guild_settings,
                "get_bot_configuration_message",
                return_value=(10, 555),
            ),
            patch.object(
                configuration_panel,
                "_resolve_publication_channel",
                new=AsyncMock(return_value=channel),
            ),
            patch.object(
                configuration_panel,
                "_build_bot_configuration_panel",
                return_value=final_embed,
            ),
            patch.object(configuration_panel, "_persist_publication", side_effect=persist),
            patch.object(configuration_panel.runtime_state, "set_status"),
        ):
            message_id = await configuration_panel._complete_publication(guild, record)

        message = sent["message"]
        self.assertEqual(555, message_id)
        self.assertNotIn(marker, str(sent["content"]))
        self.assertEqual(message_checkpoints.stable_nonce(marker), sent["nonce"])
        self.assertEqual(["send", "message_ready", "edit"], events[:3])
        self.assertEqual("", message.content)
        self.assertEqual([final_embed], message.embeds)
        self.assertTrue(persist_calls[-1]["checkpoints_removed"])

    async def test_completed_legacy_footer_is_removed_without_changing_embed(self) -> None:
        marker = configuration_panel._publication_marker(30)
        legacy_embed = discord.Embed(
            title="Bot Configuration",
            description="Keep this configuration snapshot",
        )
        legacy_embed.set_footer(text=marker)
        message = SimpleNamespace(
            id=555,
            content="Visible note",
            embeds=[legacy_embed],
            nonce=None,
            edit=AsyncMock(),
        )
        channel = SimpleNamespace(
            fetch_message=AsyncMock(return_value=message),
            history=MagicMock(),
        )
        guild = SimpleNamespace(id=30)
        record = SimpleNamespace(
            external_id="panel",
            payload={"channel_id": 10, "message_id": 555, "marker": marker},
        )

        with (
            patch.object(
                configuration_panel,
                "_resolve_publication_channel",
                new=AsyncMock(return_value=channel),
            ),
            patch.object(configuration_panel, "_persist_publication") as persist,
        ):
            cleaned = await configuration_panel._clean_completed_publication_checkpoint(
                guild,
                record,
            )

        self.assertTrue(cleaned)
        channel.history.assert_not_called()
        edited = message.edit.await_args.kwargs
        self.assertNotIn("content", edited)
        self.assertEqual("Visible note", message.content)
        self.assertEqual("Bot Configuration", edited["embeds"][0].title)
        self.assertEqual("Keep this configuration snapshot", edited["embeds"][0].description)
        self.assertIsNone(edited["embeds"][0].footer.text)
        self.assertTrue(persist.call_args.kwargs["checkpoints_removed"])
        self.assertEqual("completed", persist.call_args.kwargs["status"])

    async def test_active_panel_is_cleaned_without_a_publication_record(self) -> None:
        marker = configuration_panel._publication_marker(30)
        embed = discord.Embed(title="Bot Configuration")
        embed.set_footer(text=marker)
        message = SimpleNamespace(
            author=SimpleNamespace(id=100),
            content="",
            embeds=[embed],
            edit=AsyncMock(),
        )
        channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
        guild = SimpleNamespace(id=30, me=SimpleNamespace(id=100))

        with (
            patch.object(
                configuration_panel.guild_settings,
                "get_bot_configuration_message",
                return_value=(10, 555),
            ),
            patch.object(
                configuration_panel,
                "_resolve_publication_channel",
                new=AsyncMock(return_value=channel),
            ),
        ):
            cleaned = await configuration_panel._clean_active_configuration_panel_checkpoint(guild)

        self.assertTrue(cleaned)
        retained_embed = message.edit.await_args.kwargs["embeds"][0]
        self.assertEqual("Bot Configuration", retained_embed.title)
        self.assertIsNone(retained_embed.footer.text)


if __name__ == "__main__":
    unittest.main()
