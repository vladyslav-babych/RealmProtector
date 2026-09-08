import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from src.realm_protector.bot import ticket_configuration, tickets
from src.realm_protector.infrastructure import runtime_state


def _record(kind="ticket", *, status="open", payload=None, external_id="300"):
    return runtime_state.RuntimeRecord(
        kind=kind,
        guild_id=1,
        external_id=external_id,
        payload=payload or {},
        status=status,
        updated_at="now",
    )


def _message(message_id, *, title="Applications", description="Old text", author=99):
    return SimpleNamespace(
        id=message_id,
        author=SimpleNamespace(id=author),
        embeds=[discord.Embed(title=title, description=description)],
        edit=AsyncMock(),
        components=[],
    )


class TicketConfigurationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.panel = {
            "id": "applications",
            "active": True,
            "panel_name": "Applications",
            "panel_message": "Apply here",
            "ticket_message": "Updated welcome",
            "panel_channel_id": 10,
            "panel_message_id": 20,
            "panel_destination_channel_id": 10,
            "ticket_category_id": 30,
            "ticket_archive_channel_id": 40,
            "management_role_ids": [50],
        }
        self.guild = SimpleNamespace(
            id=1,
            me=SimpleNamespace(id=99),
            _state=SimpleNamespace(_get_client=lambda: SimpleNamespace()),
            get_channel=MagicMock(),
            fetch_channel=AsyncMock(),
        )

    async def test_open_panel_and_management_preserve_existing_controls(self):
        open_view = tickets.TicketOpenView(None)
        self.assertEqual(
            [
                getattr(item, "label", getattr(getattr(item, "item", None), "label", None))
                for item in open_view.children
            ],
            ["Open Ticket", "Update Config"],
        )
        manage = tickets.ManagePanelsView(None, self.guild, 2, [self.panel])
        labels = [
            getattr(item, "label", getattr(getattr(item, "item", None), "label", None))
            for item in manage.children
        ]
        self.assertIn("Send Panel Again", labels)
        self.assertIn("Delete Panel", labels)
        self.assertIn("Update Config", labels)

    async def test_refresh_public_panel_rebuilds_current_title_text_and_buttons(self):
        message = _message(20)
        channel = MagicMock(spec=discord.TextChannel)
        channel.fetch_message = AsyncMock(return_value=message)
        self.guild.get_channel.return_value = channel
        with (
            patch.object(ticket_configuration.ADAPTER, "load", return_value=self.panel),
            patch.object(runtime_state, "list_records", return_value=[]),
        ):
            warnings = await ticket_configuration._refresh_public_panel(self.guild, "applications")
        self.assertEqual(warnings, [])
        values = message.edit.await_args.kwargs
        self.assertEqual(values["embed"].title, "Applications")
        self.assertEqual(values["embed"].description, "Apply here")
        self.assertIsInstance(values["view"], tickets.TicketOpenView)
        self.assertFalse(values["allowed_mentions"].users)
        channel.send.assert_not_called()

    async def test_disabled_panel_is_not_reactivated_after_fetch(self):
        message = _message(20)
        channel = MagicMock(spec=discord.TextChannel)
        channel.fetch_message = AsyncMock(return_value=message)
        self.guild.get_channel.return_value = channel
        with (
            patch.object(ticket_configuration.ADAPTER, "load", side_effect=[self.panel, None]),
            patch.object(runtime_state, "list_records", return_value=[]),
        ):
            await ticket_configuration._refresh_public_panel(self.guild, "applications")
        message.edit.assert_not_awaited()

    async def test_deleted_public_message_is_not_reposted(self):
        channel = MagicMock(spec=discord.TextChannel)
        channel.fetch_message = AsyncMock(
            side_effect=discord.NotFound(SimpleNamespace(status=404, reason="gone"), "gone")
        )
        self.guild.get_channel.return_value = channel
        with (
            patch.object(ticket_configuration.ADAPTER, "load", return_value=self.panel),
            patch.object(runtime_state, "list_records", return_value=[]),
        ):
            self.assertEqual(
                await ticket_configuration._refresh_public_panel(self.guild, "applications"), []
            )
        channel.send.assert_not_called()

    async def test_welcome_refresh_preserves_character_applicant_stats_and_controls(self):
        message = _message(301, title="Ticket: AlbionPlayer")
        message.embeds[0].add_field(name="Applicant", value="<@71>")
        message.embeds[0].add_field(name="Management team", value="Original managers")
        message.embeds[0].set_footer(text="Existing footer")
        extra_embed = discord.Embed(title="Statistics", description="PvE: 1234")
        message.embeds.append(extra_embed)
        channel = MagicMock(spec=discord.TextChannel)
        channel.name = "open-albionplayer"
        channel.fetch_message = AsyncMock(return_value=message)
        self.guild.get_channel.return_value = channel
        record = _record(
            payload={
                "panel_id": "applications",
                "control_message_id": 301,
                "character_stats": {"KillFame": 123},
                "opener_id": 71,
                "lifecycle_config": {"management_role_ids": [70]},
            }
        )
        with (
            patch.object(runtime_state, "get_record", return_value=record),
            patch.object(runtime_state, "upsert_record") as persisted,
        ):
            result = await ticket_configuration._refresh_ticket_welcome(
                self.guild, record, self.panel
            )
        self.assertEqual(result, [])
        values = message.edit.await_args.kwargs
        self.assertNotIn("view", values)
        self.assertNotIn("content", values)
        embed = values["embeds"][0]
        self.assertEqual(embed.title, "Ticket: AlbionPlayer")
        self.assertEqual(embed.description, "Updated welcome")
        self.assertEqual(embed.fields[0].value, "<@71>")
        self.assertEqual(embed.fields[1].value, "Original managers")
        self.assertEqual(embed.footer.text, "Existing footer")
        self.assertEqual(values["embeds"][1].to_dict(), extra_embed.to_dict())
        self.assertEqual(message.embeds[0].description, "Old text")
        payload = persisted.call_args.args[3]
        self.assertEqual(payload["character_stats"], {"KillFame": 123})
        self.assertEqual(payload["lifecycle_config"], {"management_role_ids": [70]})

    async def test_archiving_ticket_is_not_edited(self):
        record = _record(status="archiving", payload={"control_message_id": 301})
        with patch.object(runtime_state, "get_record", return_value=record):
            self.assertEqual(
                await ticket_configuration._refresh_ticket_welcome(self.guild, record, self.panel),
                [],
            )
        self.guild.get_channel.assert_not_called()

    async def test_welcome_id_is_resolved_from_durable_creation(self):
        message = _message(301)
        channel = MagicMock(spec=discord.TextChannel)
        channel.name = "open-player"
        channel.fetch_message = AsyncMock(return_value=message)
        self.guild.get_channel.return_value = channel
        record = _record(payload={"creation_id": "creation"})
        creation = _record(
            "ticket_creation", status="completed", payload={"control_message_id": 301}
        )
        with (
            patch.object(runtime_state, "get_record", side_effect=[record, creation]),
            patch.object(runtime_state, "upsert_record"),
        ):
            await ticket_configuration._refresh_ticket_welcome(self.guild, record, self.panel)
        channel.fetch_message.assert_awaited_once_with(301)
        channel.history.assert_not_called()

    async def test_non_bot_welcome_is_never_edited(self):
        message = _message(301, author=71)
        channel = MagicMock(spec=discord.TextChannel)
        channel.name = "open-player"
        channel.fetch_message = AsyncMock(return_value=message)
        self.guild.get_channel.return_value = channel
        record = _record(payload={"control_message_id": 301})
        with patch.object(runtime_state, "get_record", return_value=record):
            warnings = await ticket_configuration._refresh_ticket_welcome(
                self.guild, record, self.panel
            )
        self.assertIn("not authored", warnings[0])
        message.edit.assert_not_awaited()

    async def test_legacy_welcome_recovery_requires_unique_bot_owned_close_panel(self):
        expected = _message(301)
        expected.components = [
            SimpleNamespace(children=[SimpleNamespace(custom_id="tickets:close")])
        ]
        spoof = _message(302, author=71)
        spoof.components = expected.components
        unrelated = _message(303)

        async def history():
            for message in [unrelated, spoof, expected]:
                yield message

        channel = SimpleNamespace(history=lambda **kwargs: history())
        result = await ticket_configuration._find_legacy_welcome(self.guild, channel)
        self.assertIs(result, expected)

        async def ambiguous_history():
            yield expected
            yield expected

        channel.history = lambda **kwargs: ambiguous_history()
        self.assertIsNone(await ticket_configuration._find_legacy_welcome(self.guild, channel))

    async def test_validation_rejects_public_archive(self):
        category = MagicMock(spec=discord.CategoryChannel)
        archive = MagicMock(spec=discord.TextChannel)
        destination = MagicMock(spec=discord.TextChannel)
        self.guild.default_role = SimpleNamespace(id=1)
        archive.permissions_for.return_value = SimpleNamespace(view_channel=True)
        self.guild.get_channel.side_effect = [category, archive, destination]
        with (
            patch.object(ticket_configuration.ADAPTER, "load", return_value=self.panel),
            patch.object(tickets, "_resolve_management_roles", return_value=([], None)),
        ):
            with self.assertRaisesRegex(ValueError, "private from @everyone"):
                await ticket_configuration.ADAPTER.validate(self.guild, "applications", self.panel)

    async def test_validation_rejects_invalid_management_roles(self):
        with (
            patch.object(ticket_configuration.ADAPTER, "load", return_value=self.panel),
            patch.object(
                tickets, "_resolve_management_roles", return_value=([], "Role was deleted")
            ),
        ):
            with self.assertRaisesRegex(ValueError, "Role was deleted"):
                await ticket_configuration.ADAPTER.validate(self.guild, "applications", self.panel)
        self.guild.get_channel.assert_not_called()

    async def test_validation_requires_archive_and_destination_permissions(self):
        self.guild.default_role = SimpleNamespace(id=1)
        category = MagicMock(spec=discord.CategoryChannel)
        category.permissions_for.return_value = discord.Permissions.all()
        archive = MagicMock(spec=discord.TextChannel)
        archive.permissions_for.side_effect = [
            discord.Permissions.none(),
            discord.Permissions.all(),
        ]
        destination = MagicMock(spec=discord.TextChannel)
        permissions = discord.Permissions.all()
        permissions.embed_links = False
        destination.permissions_for.return_value = permissions
        self.guild.get_channel.side_effect = [category, archive, destination]
        with (
            patch.object(ticket_configuration.ADAPTER, "load", return_value=self.panel),
            patch.object(tickets, "_resolve_management_roles", return_value=([], None)),
        ):
            with self.assertRaisesRegex(ValueError, "Embed Links"):
                await ticket_configuration.ADAPTER.validate(self.guild, "applications", self.panel)

    def test_save_snapshots_lifecycle_and_preserves_stored_message_ids(self):
        record = _record(payload={"panel_id": "applications"})
        previous = dict(self.panel)
        values = dict(
            self.panel,
            management_role_ids=[60],
            ticket_archive_channel_id=41,
            panel_message_id=99999,
        )
        with (
            patch.object(ticket_configuration.ADAPTER, "load", return_value=previous),
            patch.object(runtime_state, "list_records", side_effect=[[record], []]),
            patch.object(runtime_state, "upsert_record") as snapshot,
            patch.object(tickets, "_save_panel") as saved,
        ):
            ticket_configuration.ADAPTER.save(1, "applications", values)
        self.assertEqual(
            snapshot.call_args.args[3]["lifecycle_config"]["management_role_ids"], [50]
        )
        self.assertEqual(
            snapshot.call_args.args[3]["lifecycle_config"]["ticket_archive_channel_id"], 40
        )
        self.assertEqual(saved.call_args.args[1]["management_role_ids"], [60])
        self.assertEqual(saved.call_args.args[1]["panel_message_id"], 20)

    def test_existing_lifecycle_snapshot_overrides_latest_panel_on_close(self):
        record = _record(
            payload={
                "lifecycle_config": {
                    "management_role_ids": [70],
                    "closed_ticket_category_id": 80,
                }
            }
        )
        with patch.object(runtime_state, "get_record", return_value=record):
            panel = tickets._panel_for_existing_ticket(1, 300, self.panel)
        self.assertEqual(panel["management_role_ids"], [70])
        self.assertEqual(tickets._get_panel_close_mode(panel), "legacy_category")
        self.assertEqual(panel["ticket_message"], "Updated welcome")

    async def test_destination_move_commits_then_disables_previous_message(self):
        self.panel["panel_destination_channel_id"] = 11
        channel = MagicMock(spec=discord.TextChannel)
        self.guild.get_channel.return_value = channel

        async def publish(bot, guild, destination, candidate, **kwargs):
            candidate.update(panel_channel_id=11, panel_message_id=21)
            return _message(21), "publication"

        with (
            patch.object(ticket_configuration.ADAPTER, "load", return_value=self.panel),
            patch.object(runtime_state, "list_records", return_value=[]),
            patch.object(tickets, "_post_pending_ticket_panel", side_effect=publish) as published,
            patch.object(tickets, "_save_panel") as saved,
            patch.object(
                tickets, "_disable_previous_ticket_panel_message", return_value=True
            ) as disabled,
            patch.object(tickets, "_finish_panel_publish") as finished,
        ):
            result = await ticket_configuration._refresh_public_panel(self.guild, "applications")
        self.assertEqual(result, [])
        self.assertEqual(published.await_args.kwargs["operation"], "resend")
        self.assertEqual(saved.call_args.args[1]["panel_message_id"], 21)
        self.assertEqual(disabled.await_args.args[1]["previous_panel_message_id"], 20)
        finished.assert_called_once_with(1, "publication")

    async def test_failed_old_panel_cleanup_prevents_duplicate_move_on_retry(self):
        self.panel["panel_destination_channel_id"] = 11
        pending = _record(
            "ticket_panel_publish",
            payload={
                "panel_id": "applications",
                "panel": self.panel,
            },
        )
        with (
            patch.object(runtime_state, "list_records", return_value=[pending]),
            patch.object(tickets, "_ticket_panel_publication_was_committed", return_value=True),
            patch.object(tickets, "_disable_previous_ticket_panel_message", return_value=False),
            patch.object(tickets, "_post_pending_ticket_panel") as published,
        ):
            result = await ticket_configuration._refresh_public_panel(self.guild, "applications")
        self.assertTrue(result)
        published.assert_not_called()

    async def test_failed_refresh_is_reported_but_other_tickets_are_refreshed(self):
        records = [_record(payload={"panel_id": "applications"})]
        error = discord.Forbidden(SimpleNamespace(status=403, reason="forbidden"), "forbidden")
        with (
            patch.object(ticket_configuration.ADAPTER, "load", return_value=self.panel),
            patch.object(ticket_configuration, "_refresh_public_panel", side_effect=error),
            patch.object(runtime_state, "list_records", return_value=records),
            patch.object(
                ticket_configuration, "_refresh_ticket_welcome", return_value=[]
            ) as welcome,
        ):
            warnings = await ticket_configuration.ADAPTER.refresh(self.guild, "applications")
        self.assertEqual(len(warnings), 1)
        welcome.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
