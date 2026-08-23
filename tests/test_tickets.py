import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from src.realm_protector.bot import character_picker, tickets
from src.realm_protector.services import albion_characters


def _legacy_archive_marker_embed(marker: str) -> discord.Embed:
    embed = discord.Embed()
    embed.set_footer(text=marker)
    return embed


class TicketPanelCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    def test_ticket_creation_nonce_is_hidden_stable_metadata(self) -> None:
        control = tickets._ticket_creation_nonce("operation-one", "control")

        self.assertEqual(
            control,
            tickets._ticket_creation_nonce("operation-one", "control"),
        )
        self.assertNotEqual(
            control,
            tickets._ticket_creation_nonce("operation-one", "stats"),
        )

    async def test_legacy_visible_creation_footer_is_removed(self) -> None:
        for part in ("control", "stats"):
            with self.subTest(part=part):
                embed = discord.Embed(title="Ticket")
                embed.set_footer(text=tickets._ticket_creation_marker("operation-one", part))
                message = SimpleNamespace(embeds=[embed], edit=AsyncMock())

                await tickets._remove_legacy_ticket_creation_footer(
                    message,
                    "operation-one",
                    part,
                )

                edited_embed = message.edit.await_args.kwargs["embeds"][0]
                self.assertIsNone(edited_embed.footer.text)

    async def test_ticket_creation_recovery_uses_nonce_without_visible_footer(self) -> None:
        expected = SimpleNamespace(
            nonce=tickets._ticket_creation_nonce("operation-one", "stats"),
            embeds=[discord.Embed(title="General Info")],
        )

        class HistoryChannel:
            fetch_message = AsyncMock()

            def history(self, *, limit):
                async def messages():
                    yield SimpleNamespace(nonce=123, embeds=[])
                    yield expected

                return messages()

        channel = HistoryChannel()
        recovered = await tickets._find_ticket_creation_message(
            channel,
            "operation-one",
            "stats",
            None,
        )

        self.assertIs(expected, recovered)
        self.assertIsNone(expected.embeds[0].footer.text)
        self.assertFalse(channel.fetch_message.called)

    async def test_ticket_creation_recovery_ignores_another_authors_checkpoint(self) -> None:
        nonce = tickets._ticket_creation_nonce("operation-one", "stats")
        spoof = SimpleNamespace(author=SimpleNamespace(id=999), nonce=nonce, embeds=[])
        expected = SimpleNamespace(author=SimpleNamespace(id=100), nonce=nonce, embeds=[])

        class HistoryChannel:
            guild = SimpleNamespace(me=SimpleNamespace(id=100))
            fetch_message = AsyncMock()

            def history(self, *, limit):
                async def messages():
                    yield spoof
                    yield expected

                return messages()

        recovered = await tickets._find_ticket_creation_message(
            HistoryChannel(),
            "operation-one",
            "stats",
            None,
        )

        self.assertIs(expected, recovered)

    async def test_persisted_message_id_does_not_require_a_footer(self) -> None:
        expected = SimpleNamespace(nonce=None, embeds=[])
        channel = SimpleNamespace(
            fetch_message=AsyncMock(return_value=expected),
            history=MagicMock(),
        )

        recovered = await tickets._find_ticket_creation_message(
            channel,
            "operation-one",
            "control",
            123,
        )

        self.assertIs(expected, recovered)
        channel.history.assert_not_called()

    async def test_completed_legacy_ticket_messages_are_cleaned_on_restart(self) -> None:
        operation_id = "operation-one"
        messages = []
        for part in ("control", "stats"):
            embed = discord.Embed(title=part.title())
            embed.set_footer(text=tickets._ticket_creation_marker(operation_id, part))
            messages.append(SimpleNamespace(embeds=[embed], edit=AsyncMock()))
        channel = SimpleNamespace(
            fetch_message=AsyncMock(side_effect=messages),
            history=MagicMock(),
        )
        record = SimpleNamespace(
            guild_id=707,
            external_id=operation_id,
            payload={
                "channel_id": 303,
                "control_message_id": 401,
                "stats_message_id": 402,
            },
            status="completed",
        )

        with (
            patch.object(
                tickets,
                "_fetch_guild_channel",
                new=AsyncMock(return_value=channel),
            ),
            patch.object(tickets, "_persist_ticket_creation") as persist,
        ):
            await tickets._clean_completed_ticket_creation_checkpoints(
                SimpleNamespace(id=707),
                record,
            )

        for message in messages:
            edited_embed = message.edit.await_args.kwargs["embeds"][0]
            self.assertIsNone(edited_embed.footer.text)
        persisted_payload = persist.call_args.args[2]
        self.assertTrue(persisted_payload["visible_markers_removed"])
        self.assertTrue(persisted_payload["discord_checkpoints_removed"])
        self.assertEqual("completed", persist.call_args.kwargs["status"])

    async def test_active_ticket_is_cleaned_without_creation_action_row(self) -> None:
        operation_id = "operation-one"
        messages = []
        for part in ("control", "stats"):
            embed = discord.Embed(title=part.title())
            embed.set_footer(text=tickets._ticket_creation_marker(operation_id, part))
            messages.append(
                SimpleNamespace(
                    author=SimpleNamespace(id=100),
                    content="",
                    embeds=[embed],
                    nonce=None,
                    edit=AsyncMock(),
                )
            )

        class HistoryChannel:
            guild = SimpleNamespace(me=SimpleNamespace(id=100))
            fetch_message = AsyncMock()

            def history(self, *, limit):
                async def entries():
                    for message in messages:
                        yield message

                return entries()

        record = SimpleNamespace(
            guild_id=707,
            external_id="303",
            status="open",
            payload={"creation_id": operation_id},
        )

        with patch.object(tickets.runtime_state, "upsert_record") as upsert:
            cleaned = await tickets._clean_active_ticket_creation_checkpoints(
                HistoryChannel(),
                record,
            )

        self.assertTrue(cleaned)
        for message in messages:
            self.assertIsNone(message.edit.await_args.kwargs["embeds"][0].footer.text)
        self.assertTrue(upsert.call_args.args[3]["creation_checkpoints_removed"])

    def test_ticket_names_and_topics_use_the_character_without_a_number(self) -> None:
        self.assertEqual(
            "open-player-one",
            tickets._build_ticket_channel_name("open", "Player One"),
        )
        topic = tickets._build_ticket_topic_with_character(
            "panel-a",
            404,
            "player-one",
            "Player One",
            "albion-player-id",
        )
        self.assertEqual(
            "panel_id=panel-a;opener_id=404;opener_slug=player-one;"
            "character=Player%20One;albion_id=albion-player-id",
            topic,
        )

    async def test_ticket_creation_checkpoint_is_hidden_then_removed_from_topic(self) -> None:
        operation_id = "operation-one"
        topic = tickets._build_ticket_topic_with_character(
            "panel-a",
            404,
            "player-one",
            "Player One",
            "albion-player-id",
            creation_id=operation_id,
        )
        channel = SimpleNamespace(topic=topic, edit=AsyncMock())
        guild = SimpleNamespace(text_channels=[channel])

        self.assertNotIn("creation_id", topic)
        self.assertNotIn(operation_id, topic)
        self.assertIs(
            channel,
            await tickets._find_ticket_creation_channel(guild, operation_id),
        )
        await tickets._remove_ticket_creation_topic_checkpoint(channel, operation_id)

        cleaned = channel.edit.await_args.kwargs["topic"]
        self.assertEqual(
            "panel_id=panel-a;opener_id=404;opener_slug=player-one;"
            "character=Player%20One;albion_id=albion-player-id",
            cleaned,
        )

    async def test_character_picker_shows_three_profiles_and_numbered_buttons(self) -> None:
        options = [
            albion_characters.AlbionCharacterOption(
                {
                    "Id": f"player-{position}",
                    "Name": f"Player {position}",
                    "GuildName": f"Guild {position}",
                    "KillFame": position * 1_000,
                    "DeathFame": position * 100,
                    "FameRatio": position,
                },
                position * 10_000,
            )
            for position in range(1, 4)
        ]

        embed = character_picker.build_character_selection_embed(options)
        view = tickets._TicketCharacterSelectionView(
            None,
            user_id=404,
            panel_id="panel-a",
            character_options=options,
        )

        self.assertEqual("Select your character", embed.title)
        self.assertEqual(
            ["1. Player 1", "2. Player 2", "3. Player 3"],
            [field.name for field in embed.fields],
        )
        self.assertIn("**PvE Fame:** 30,000", embed.fields[2].value)
        self.assertEqual(["1", "2", "3", "Cancel"], [item.label for item in view.children])
        self.assertFalse(any(item.disabled for item in view.children[:3]))

    async def test_missing_character_results_disable_unused_numbered_buttons(self) -> None:
        option = albion_characters.AlbionCharacterOption(
            {"Id": "only", "Name": "Only Player"},
            0,
        )
        view = tickets._TicketCharacterSelectionView(
            None,
            user_id=404,
            panel_id="panel-a",
            character_options=[option],
        )

        self.assertEqual([False, True, True], [item.disabled for item in view.children[:3]])

    def test_active_panel_lookup_fails_closed_without_main_configuration(self) -> None:
        with (
            patch.object(
                tickets.guild_settings,
                "get_target_guild",
                return_value=None,
            ),
            patch.object(tickets, "_get_panel_by_id") as get_panel,
        ):
            panel = tickets._get_active_panel_by_id(42, "panel-a")

        self.assertIsNone(panel)
        get_panel.assert_not_called()

    def test_close_mode_prefers_archive_and_falls_back_to_legacy_category(self) -> None:
        self.assertEqual(
            "archive",
            tickets._get_panel_close_mode(
                {
                    "ticket_archive_channel_id": "101",
                    "closed_ticket_category_id": "202",
                }
            ),
        )
        self.assertEqual(
            "legacy_category",
            tickets._get_panel_close_mode(
                {
                    "ticket_archive_channel_id": "not-a-channel-id",
                    "closed_ticket_category_id": 202,
                }
            ),
        )
        self.assertEqual("unconfigured", tickets._get_panel_close_mode({}))

    def test_manage_embed_explains_how_to_replace_a_legacy_panel(self) -> None:
        category = SimpleNamespace(name="Closed tickets", mention="#closed-tickets")

        class FakeGuild:
            def get_channel(self, channel_id):
                return category if channel_id == 202 else None

            def get_role(self, role_id):
                return None

        embed = tickets._build_manage_embed(
            FakeGuild(),
            [
                {
                    "id": "legacy-panel",
                    "panel_name": "Applications",
                    "closed_ticket_category_id": 202,
                }
            ],
            "legacy-panel",
        )
        fields = {field.name: field.value for field in embed.fields}

        self.assertEqual("Closed tickets", fields["Legacy closed category"])
        self.assertIn("Create an archive-channel panel", fields["Migration required"])
        self.assertIn("Existing tickets will remain closable", fields["Migration required"])


class TicketPanelPublicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_storage_failure_prevents_discord_panel_post(self) -> None:
        destination = SimpleNamespace(send=AsyncMock())
        with patch.object(
            tickets,
            "_record_panel_publish",
            side_effect=OSError("database unavailable"),
        ):
            with self.assertRaises(tickets._PanelPublishError):
                await tickets._post_pending_ticket_panel(
                    None,
                    SimpleNamespace(id=707),
                    destination,
                    {"id": "panel-a"},
                    operation="create",
                )

        destination.send.assert_not_awaited()

    async def test_panel_becomes_functional_only_after_message_id_is_recorded(self) -> None:
        statuses = []
        message = SimpleNamespace(
            id=303,
            channel=SimpleNamespace(id=202),
            edit=AsyncMock(),
            delete=AsyncMock(),
        )
        destination = SimpleNamespace(send=AsyncMock(return_value=message))

        def record(_guild_id, _operation_id, panel, *, operation, status):
            statuses.append((status, int(panel.get("panel_message_id") or 0)))

        panel = {
            "id": "panel-a",
            "panel_name": "Applications",
            "panel_message": "Open an application.",
        }
        with patch.object(tickets, "_record_panel_publish", side_effect=record):
            posted, _operation_id = await tickets._post_pending_ticket_panel(
                None,
                SimpleNamespace(id=707),
                destination,
                panel,
                operation="create",
            )

        self.assertIs(message, posted)
        self.assertEqual(
            [("prepared", 0), ("placeholder_created", 303), ("ready_to_commit", 303)],
            statuses,
        )
        placeholder_options = destination.send.await_args.kwargs
        self.assertTrue(placeholder_options["content"].startswith("Preparing ticket panel."))
        self.assertNotIn(tickets._PANEL_PUBLISH_MARKER_PREFIX, placeholder_options["content"])
        self.assertTrue(
            tickets._message_has_panel_publish_marker(
                SimpleNamespace(
                    content=placeholder_options["content"],
                    nonce=None,
                    embeds=[],
                ),
                _operation_id,
            )
        )
        self.assertIn("nonce", placeholder_options)
        self.assertIsInstance(message.edit.await_args.kwargs["view"], tickets.TicketOpenView)

    async def test_committed_panel_footer_is_cleaned_without_publish_record(self) -> None:
        embed = discord.Embed(title="Applications")
        embed.set_footer(text=tickets._panel_publish_marker("lost-operation"))
        message = SimpleNamespace(
            author=SimpleNamespace(id=100),
            content="",
            embeds=[embed],
            edit=AsyncMock(),
        )
        channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
        guild = SimpleNamespace(
            id=707,
            me=SimpleNamespace(id=100),
            get_channel=lambda channel_id: channel if channel_id == 202 else None,
        )

        with patch.object(
            tickets,
            "_load_ticket_entry",
            return_value={
                "panels": {
                    "panel-a": {
                        "id": "panel-a",
                        "active": False,
                        "panel_channel_id": 202,
                        "panel_message_id": 303,
                    }
                }
            },
        ):
            cleaned = await tickets._clean_committed_ticket_panel_checkpoints_for_guild(guild)

        self.assertTrue(cleaned)
        retained_embed = message.edit.await_args.kwargs["embeds"][0]
        self.assertEqual("Applications", retained_embed.title)
        self.assertIsNone(retained_embed.footer.text)

    async def test_restart_deletes_uncommitted_ticket_panel(self) -> None:
        message = SimpleNamespace(delete=AsyncMock())
        channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
        guild = SimpleNamespace(id=707, get_channel=lambda channel_id: channel)
        bot = SimpleNamespace(get_guild=lambda guild_id: guild)
        record = SimpleNamespace(
            kind=tickets._PANEL_PUBLISH_RUNTIME_KIND,
            guild_id=707,
            external_id="operation-a",
            payload={
                "panel_id": "panel-a",
                "panel": {
                    "id": "panel-a",
                    "panel_channel_id": 202,
                    "panel_message_id": 303,
                },
            },
            status="ready_to_commit",
        )
        with (
            patch.object(
                tickets.runtime_state,
                "list_records",
                return_value=[record],
            ),
            patch.object(tickets, "_get_panel_by_id", return_value=None),
            patch.object(tickets.runtime_state, "delete_record") as delete_record,
        ):
            await tickets.reconcile_ticket_panel_publications(bot)

        message.delete.assert_awaited_once_with()
        delete_record.assert_called_once_with(
            tickets._PANEL_PUBLISH_RUNTIME_KIND,
            707,
            "operation-a",
        )

    async def test_restart_finds_hidden_ticket_panel_placeholder_without_saved_id(self) -> None:
        operation_id = "operation-a"
        marker = tickets._panel_publish_marker(operation_id)
        placeholder = SimpleNamespace(
            content=tickets.message_checkpoints.content_with_checkpoint(
                "Preparing ticket panel.",
                marker,
            ),
            nonce=None,
            embeds=[],
        )

        class HistoryChannel:
            def history(self, *, limit):
                self.limit = limit

                async def messages():
                    yield placeholder

                return messages()

        channel = HistoryChannel()
        guild = SimpleNamespace(
            get_channel=lambda channel_id: channel if channel_id == 202 else None
        )
        record = SimpleNamespace(
            external_id=operation_id,
            payload={
                "panel": {
                    "id": "panel-a",
                    "panel_destination_channel_id": 202,
                },
            },
        )

        resolved = await tickets._resolve_pending_ticket_panel_message(guild, record)

        self.assertIs(placeholder, resolved)
        self.assertEqual(tickets._FALLBACK_HISTORY_LIMIT, channel.limit)

    async def test_ticket_panel_recovery_ignores_another_authors_checkpoint(self) -> None:
        operation_id = "operation-a"
        marker = tickets._panel_publish_marker(operation_id)
        spoof = SimpleNamespace(
            author=SimpleNamespace(id=999),
            content=tickets.message_checkpoints.content_with_checkpoint(None, marker),
            nonce=None,
            embeds=[],
        )
        expected = SimpleNamespace(
            author=SimpleNamespace(id=100),
            content=tickets.message_checkpoints.content_with_checkpoint(None, marker),
            nonce=None,
            embeds=[],
        )

        class HistoryChannel:
            def history(self, *, limit):
                async def messages():
                    yield spoof
                    yield expected

                return messages()

        channel = HistoryChannel()
        guild = SimpleNamespace(
            me=SimpleNamespace(id=100),
            get_channel=lambda channel_id: channel if channel_id == 202 else None,
        )
        record = SimpleNamespace(
            external_id=operation_id,
            payload={"panel": {"panel_destination_channel_id": 202}},
        )

        resolved = await tickets._resolve_pending_ticket_panel_message(guild, record)

        self.assertIs(expected, resolved)


class LegacyTicketCloseTests(unittest.IsolatedAsyncioTestCase):
    async def test_closes_in_legacy_category_and_revokes_applicant_writes(self) -> None:
        class FakeCategory:
            pass

        category = FakeCategory()
        opener = SimpleNamespace(display_name="Player One")
        channel = SimpleNamespace(
            id=303,
            edit=AsyncMock(),
            set_permissions=AsyncMock(),
            send=AsyncMock(),
        )

        class FakeGuild:
            def get_member(self, member_id):
                return opener if member_id == 404 else None

            def get_channel(self, channel_id):
                return category if channel_id == 202 else None

        interaction = SimpleNamespace(
            guild=FakeGuild(),
            channel=channel,
            user=SimpleNamespace(mention="<@505>"),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        with patch.object(tickets.discord, "CategoryChannel", FakeCategory):
            await tickets._close_legacy_ticket(
                interaction,
                {"closed_ticket_category_id": 202},
                {
                    "opener_id": "404",
                    "opener_slug": "player-one",
                },
            )

        interaction.response.defer.assert_not_awaited()
        channel.set_permissions.assert_awaited_once_with(
            opener,
            send_messages=False,
            add_reactions=False,
        )
        channel.edit.assert_awaited_once_with(
            name="closed-player-one",
            category=category,
        )
        channel.send.assert_awaited_once_with(
            "Ticket closed by <@505>.",
            allowed_mentions=tickets._NO_MENTIONS,
        )
        interaction.followup.send.assert_awaited_once_with(
            "Ticket closed.",
            ephemeral=True,
        )

    async def test_renames_when_legacy_category_is_no_longer_available(self) -> None:
        channel = SimpleNamespace(
            id=303,
            edit=AsyncMock(),
            set_permissions=AsyncMock(),
            send=AsyncMock(),
        )
        interaction = SimpleNamespace(
            guild=SimpleNamespace(
                get_member=lambda member_id: None,
                get_channel=lambda channel_id: None,
            ),
            channel=channel,
            user=SimpleNamespace(mention="<@505>"),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await tickets._close_legacy_ticket(
            interaction,
            {"closed_ticket_category_id": 202},
            {"opener_id": "404"},
        )

        channel.set_permissions.assert_not_awaited()
        channel.edit.assert_awaited_once_with(name="closed-user")
        interaction.followup.send.assert_awaited_once_with(
            "Ticket closed.",
            ephemeral=True,
        )

    async def test_close_button_prefers_archive_when_both_schema_fields_exist(self) -> None:
        class FakeTextChannel:
            id = 606
            name = "open-player"
            topic = "panel_id=panel-a;opener_id=404;opener_slug=player"

        class FakeMember:
            pass

        panel = {
            "id": "panel-a",
            "management_role_ids": [1],
            "ticket_archive_channel_id": 101,
            "closed_ticket_category_id": 202,
        }
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=707),
            channel=FakeTextChannel(),
            user=FakeMember(),
            response=SimpleNamespace(
                send_message=AsyncMock(),
                defer=AsyncMock(),
                is_done=lambda: True,
            ),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        view = tickets.TicketCloseView(None)
        close_button = next(child for child in view.children if child.custom_id == "tickets:close")
        with (
            patch.object(tickets.discord, "TextChannel", FakeTextChannel),
            patch.object(tickets.discord, "Member", FakeMember),
            patch.object(tickets, "_get_panel_by_id", return_value=panel),
            patch.object(tickets, "_has_management_access", return_value=True),
            patch.object(tickets.runtime_state, "get_record", return_value=None),
            patch.object(tickets, "_archive_ticket", new=AsyncMock()) as archive,
            patch.object(
                tickets,
                "_close_legacy_ticket",
                new=AsyncMock(),
            ) as legacy_close,
        ):
            await close_button.callback(interaction)

        archive.assert_awaited_once()
        legacy_close.assert_not_awaited()


class TicketCharacterSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_picker_loads_pve_stats_for_only_the_first_three_results(self) -> None:
        search_profiles = [
            {"Id": str(position), "Name": f"Player {position}"} for position in range(1, 5)
        ]

        async def load_profile(_function, player_id):
            return {
                "LifetimeStatistics": {
                    "PvE": {"Total": int(player_id) * 1_000},
                }
            }

        with patch.object(
            albion_characters.external_io,
            "run_albion",
            side_effect=load_profile,
        ) as run_albion:
            options = await albion_characters.load_character_options(search_profiles)

        self.assertEqual(["1", "2", "3"], [item.search_profile["Id"] for item in options])
        self.assertEqual([1_000, 2_000, 3_000], [item.pve_total for item in options])
        self.assertEqual(3, run_albion.call_count)

    async def test_second_button_opens_ticket_with_second_character(self) -> None:
        class FakeCategory:
            pass

        category = FakeCategory()
        panel = {"id": "panel-a", "ticket_category_id": 202}
        options = [
            albion_characters.AlbionCharacterOption({"Id": "one", "Name": "Player One"}, 10),
            albion_characters.AlbionCharacterOption({"Id": "two", "Name": "Player Two"}, 20),
            albion_characters.AlbionCharacterOption({"Id": "three", "Name": "Player Three"}, 30),
        ]
        view = tickets._TicketCharacterSelectionView(
            "bot-instance",
            user_id=404,
            panel_id="panel-a",
            character_options=options,
        )
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=707, get_channel=lambda channel_id: category),
            user=SimpleNamespace(id=404),
            response=SimpleNamespace(
                edit_message=AsyncMock(),
                send_message=AsyncMock(),
                defer=AsyncMock(),
                is_done=lambda: True,
            ),
            edit_original_response=AsyncMock(),
        )
        second_button = next(item for item in view.children if item.label == "2")

        with (
            patch.object(tickets.discord, "CategoryChannel", FakeCategory),
            patch.object(tickets, "_get_active_panel_by_id", return_value=panel),
            patch.object(tickets, "_find_existing_open_ticket_channel", return_value=None),
            patch.object(
                tickets,
                "_create_confirmed_ticket",
                new=AsyncMock(),
            ) as create_ticket,
        ):
            await second_button.callback(interaction)

        interaction.response.defer.assert_awaited_once_with()
        interaction.edit_original_response.assert_awaited_once_with(
            content="Opening ticket...",
            embed=None,
            view=None,
        )
        create_ticket.assert_awaited_once_with(
            "bot-instance",
            interaction,
            panel,
            options[1],
        )


class TicketRuntimeReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_lookup_prefers_open_sqlite_record(self) -> None:
        durable_channel = SimpleNamespace(id=303, mention="<#303>")
        legacy_channel = SimpleNamespace(
            id=404,
            name="open-legacy",
            topic="panel_id=panel-a;opener_id=505",
        )
        guild = SimpleNamespace(id=707, text_channels=[legacy_channel])
        record = SimpleNamespace(
            external_id="303",
            payload={"panel_id": "panel-a", "opener_id": "505"},
        )

        with (
            patch.object(
                tickets.runtime_state,
                "list_records",
                return_value=[record],
            ) as list_records,
            patch.object(
                tickets,
                "_fetch_guild_channel",
                new=AsyncMock(return_value=durable_channel),
            ) as fetch_channel,
        ):
            result = await tickets._find_existing_open_ticket_channel(
                guild,
                "panel-a",
                505,
            )

        self.assertIs(durable_channel, result)
        list_records.assert_called_once_with(
            tickets._TICKET_RUNTIME_KIND,
            guild_id=707,
            statuses=("open",),
        )
        fetch_channel.assert_awaited_once_with(guild, 303)

    async def test_existing_archive_anchor_and_thread_are_reused(self) -> None:
        class FakeThread:
            pass

        thread = FakeThread()
        thread.id = 909
        anchor = SimpleNamespace(
            id=808,
            author=SimpleNamespace(id=42),
            thread=thread,
            create_thread=AsyncMock(),
        )
        archive_channel = SimpleNamespace(
            id=606,
            fetch_message=AsyncMock(return_value=anchor),
            send=AsyncMock(),
        )
        guild = SimpleNamespace(id=707)
        source_channel = SimpleNamespace(id=505, guild=guild, name="open-player")
        record = SimpleNamespace(
            payload={"archive_message_id": 808, "archive_thread_id": 909},
        )

        with (
            patch.object(tickets.discord, "Thread", FakeThread),
            patch.object(
                tickets.runtime_state,
                "get_record",
                return_value=record,
            ),
            patch.object(tickets, "_persist_ticket") as persist_ticket,
        ):
            resolved_anchor, resolved_thread = await tickets._ensure_archive_container(
                source_channel,
                {"character": "Player"},
                archive_channel,
                bot_user_id=42,
            )

        self.assertIs(anchor, resolved_anchor)
        self.assertIs(thread, resolved_thread)
        archive_channel.send.assert_not_awaited()
        anchor.create_thread.assert_not_awaited()
        self.assertEqual(
            ["closing", "archiving"],
            [call.kwargs["status"] for call in persist_ticket.call_args_list],
        )

    async def test_new_archive_anchor_uses_only_hidden_checkpoint_metadata(self) -> None:
        class FakeThread:
            pass

        async def empty_history(**_kwargs):
            if False:
                yield None

        thread = FakeThread()
        thread.id = 909
        anchor = SimpleNamespace(id=808, thread=thread)
        archive_channel = SimpleNamespace(
            id=606,
            history=empty_history,
            send=AsyncMock(return_value=anchor),
        )
        guild = SimpleNamespace(id=707)
        source_channel = SimpleNamespace(id=505, guild=guild, name="open-player")
        marker = tickets._archive_marker(707, 505, "anchor")

        with (
            patch.object(tickets.discord, "Thread", FakeThread),
            patch.object(tickets.runtime_state, "get_record", return_value=None),
            patch.object(tickets, "_persist_ticket"),
        ):
            resolved_anchor, resolved_thread = await tickets._ensure_archive_container(
                source_channel,
                {"character": "Player"},
                archive_channel,
                bot_user_id=42,
            )

        self.assertIs(anchor, resolved_anchor)
        self.assertIs(thread, resolved_thread)
        send_options = archive_channel.send.await_args.kwargs
        self.assertNotIn("embed", send_options)
        self.assertNotIn("embeds", send_options)
        self.assertTrue(send_options["content"].startswith("Player"))
        self.assertEqual(tickets._archive_nonce(marker), send_options["nonce"])
        self.assertIn(
            tickets._archive_nonce_key(marker),
            tickets._hidden_archive_checkpoint_keys(send_options["content"]),
        )

    async def test_persisted_archive_anchor_rejects_another_author(self) -> None:
        marker = tickets._archive_marker(707, 505, "anchor")
        impostor = SimpleNamespace(id=808, author=SimpleNamespace(id=999))
        expected = SimpleNamespace(
            id=809,
            author=SimpleNamespace(id=42),
            content=tickets._archive_content_with_checkpoint("Player", marker),
            embeds=[],
            nonce=tickets._archive_nonce(marker),
        )

        async def history(**_kwargs):
            yield expected

        archive_channel = SimpleNamespace(
            fetch_message=AsyncMock(return_value=impostor),
            history=history,
        )

        recovered = await tickets._find_archive_anchor(
            archive_channel,
            guild_id=707,
            source_channel_id=505,
            archive_message_id=808,
            bot_user_id=42,
        )

        self.assertIs(expected, recovered)

    async def test_partial_transcript_resumes_without_repeating_marked_chunks(self) -> None:
        async def iter_messages(messages):
            for message in messages:
                yield message

        guild = SimpleNamespace(id=707)
        source_message = SimpleNamespace(
            id=111,
            author=SimpleNamespace(display_name="Applicant"),
            created_at=None,
            content="x" * 2000,
            attachments=[],
        )
        source_channel = SimpleNamespace(
            id=505,
            guild=guild,
            mention="<#505>",
            history=lambda **_kwargs: iter_messages([source_message]),
        )
        intro_marker = tickets._archive_marker(707, 505, "intro")
        first_chunk_marker = tickets._archive_marker(707, 505, "message:111:0")
        archived_messages = [
            SimpleNamespace(
                content="Archived from <#505> by restart recovery.",
                embeds=[_legacy_archive_marker_embed(intro_marker)],
            ),
            SimpleNamespace(
                content="already copied",
                embeds=[_legacy_archive_marker_embed(first_chunk_marker)],
            ),
        ]
        thread = SimpleNamespace(
            history=lambda **_kwargs: iter_messages(archived_messages),
            send=AsyncMock(),
        )

        with (
            patch.object(tickets.asyncio, "sleep", new=AsyncMock()),
            patch.object(tickets, "_persist_ticket") as persist_ticket,
        ):
            await tickets._copy_ticket_to_archive_once(
                source_channel,
                thread,
                {"panel_id": "panel-a", "opener_id": "404"},
                actor_label="restart recovery",
            )

        self.assertEqual(2, thread.send.await_count)
        expected_markers = [
            tickets._archive_marker(707, 505, "message:111:1"),
            tickets._archive_marker(707, 505, "complete"),
        ]
        self.assertEqual(
            [tickets._archive_nonce(marker) for marker in expected_markers],
            [call.kwargs["nonce"] for call in thread.send.await_args_list],
        )
        for call, marker in zip(
            thread.send.await_args_list,
            expected_markers,
            strict=True,
        ):
            self.assertNotIn("embed", call.kwargs)
            self.assertNotIn("embeds", call.kwargs)
            self.assertIn(
                tickets._archive_nonce_key(marker),
                tickets._hidden_archive_checkpoint_keys(call.args[0]),
            )
        self.assertEqual(
            "archived_source_remaining",
            persist_ticket.call_args.kwargs["status"],
        )

    async def test_archive_artifact_keeps_real_embed_without_checkpoint_panel(self) -> None:
        thread = SimpleNamespace(send=AsyncMock())
        marker = tickets._archive_marker(707, 505, "message:111:embed:0")
        archived_embed = discord.Embed(title="Character stats")
        checkpoints: set[str] = set()

        await tickets._send_archive_artifact_once(
            thread,
            marker,
            checkpoints,
            embeds=[archived_embed],
        )

        send_options = thread.send.await_args.kwargs
        self.assertNotIn("embed", send_options)
        self.assertEqual([archived_embed], send_options["embeds"])
        self.assertIsNone(archived_embed.footer.text)
        self.assertIn(
            tickets._archive_nonce_key(marker),
            tickets._hidden_archive_checkpoint_keys(send_options["content"]),
        )

    async def test_archive_artifact_chunks_without_losing_visible_content(self) -> None:
        thread = SimpleNamespace(send=AsyncMock())
        marker = tickets._archive_marker(707, 505, "message:111:poll")
        visible_content = "p" * 2000

        await tickets._send_archive_artifact_once(
            thread,
            marker,
            set(),
            content=visible_content,
        )

        self.assertEqual(2, thread.send.await_count)
        sent_contents = [call.kwargs["content"] for call in thread.send.await_args_list]
        self.assertTrue(all(len(content) <= 2000 for content in sent_contents))
        self.assertEqual(
            visible_content,
            "".join(
                tickets._strip_hidden_archive_checkpoints(content) for content in sent_contents
            ),
        )

    async def test_hidden_checkpoint_survives_when_discord_nonce_is_absent(self) -> None:
        async def iter_messages(messages):
            for message in messages:
                yield message

        marker = tickets._archive_marker(707, 505, "message:111:0")
        archived_message = SimpleNamespace(
            content=tickets._archive_content_with_checkpoint("copied", marker),
            embeds=[],
            nonce=None,
        )
        thread = SimpleNamespace(
            history=lambda **_kwargs: iter_messages([archived_message]),
            send=AsyncMock(),
        )

        checkpoints, legacy_contents = await tickets._read_archive_thread_state(
            thread,
            guild_id=707,
            source_channel_id=505,
        )
        await tickets._send_archive_piece_once(
            thread,
            "copied",
            marker,
            checkpoints,
            legacy_contents,
        )

        thread.send.assert_not_awaited()

    async def test_completed_archive_cleanup_preserves_transcript_embeds(self) -> None:
        async def iter_messages(messages):
            for message in messages:
                yield message

        anchor_marker = tickets._archive_marker(707, 505, "anchor")
        message_marker = tickets._archive_marker(707, 505, "message:111:embed:0")
        real_embed = discord.Embed(title="Character stats")
        anchor = SimpleNamespace(
            content=tickets._archive_content_with_checkpoint("Player", anchor_marker),
            embeds=[_legacy_archive_marker_embed(anchor_marker)],
            edit=AsyncMock(),
        )
        archived_message = SimpleNamespace(
            content=tickets._archive_content_with_checkpoint("Visible", message_marker),
            embeds=[
                real_embed,
                _legacy_archive_marker_embed(message_marker),
            ],
            edit=AsyncMock(),
        )
        thread = SimpleNamespace(
            history=lambda **_kwargs: iter_messages([archived_message]),
        )

        cleaned = await tickets._clean_completed_archive_checkpoints(
            anchor,
            thread,
        )

        self.assertTrue(cleaned)
        anchor_options = anchor.edit.await_args.kwargs
        self.assertEqual("Player", anchor_options["content"])
        self.assertEqual([], anchor_options["embeds"])
        archived_options = archived_message.edit.await_args.kwargs
        self.assertEqual("Visible", archived_options["content"])
        retained = archived_options["embeds"]
        self.assertEqual(1, len(retained))
        self.assertEqual(real_embed.to_dict(), retained[0].to_dict())

    async def test_completed_archive_cleanup_preserves_real_marked_embed(self) -> None:
        marker = tickets._archive_marker(707, 505, "message:111:embed:0")
        archived_embed = discord.Embed(title="Character stats", description="Keep me")
        archived_embed.set_footer(text=marker)
        message = SimpleNamespace(
            content="",
            embeds=[archived_embed],
            edit=AsyncMock(),
        )

        changed = await tickets._clean_archive_message_checkpoints(message)

        self.assertTrue(changed)
        retained_embed = message.edit.await_args.kwargs["embeds"][0]
        self.assertEqual("Character stats", retained_embed.title)
        self.assertEqual("Keep me", retained_embed.description)
        self.assertIsNone(retained_embed.footer.text)

    async def test_archive_cleanup_removes_copied_ticket_creation_checkpoints(self) -> None:
        creation_marker = tickets._ticket_creation_marker("operation-one", "control")
        archive_marker = tickets._archive_marker(707, 505, "message:111:embed:0")
        archived_embed = discord.Embed(title="Ticket", description="Keep this ticket data")
        archived_embed.set_footer(text=creation_marker)
        copied_content = tickets.message_checkpoints.content_with_checkpoint(
            "Visible transcript",
            creation_marker,
            nonce=tickets._ticket_creation_nonce("operation-one", "control"),
        )
        message = SimpleNamespace(
            content=tickets._archive_content_with_checkpoint(copied_content, archive_marker),
            embeds=[archived_embed],
            edit=AsyncMock(),
        )

        changed = await tickets._clean_archive_message_checkpoints(message)

        self.assertTrue(changed)
        options = message.edit.await_args.kwargs
        self.assertEqual("Visible transcript", options["content"])
        retained_embed = options["embeds"][0]
        self.assertEqual("Ticket", retained_embed.title)
        self.assertEqual("Keep this ticket data", retained_embed.description)
        self.assertIsNone(retained_embed.footer.text)

    async def test_restart_cleans_checkpoints_from_closed_archive(self) -> None:
        guild = SimpleNamespace(id=707, text_channels=[])
        bot = SimpleNamespace(user=SimpleNamespace(id=42), guilds=[guild])
        record = SimpleNamespace(
            guild_id=707,
            external_id="505",
            status="closed",
            payload={
                "archive_channel_id": 606,
                "archive_message_id": 808,
                "archive_thread_id": 909,
                "archive_checkpoint_embeds_removed": True,
                # Older releases set this boolean before copied ticket-creation
                # footers were included in archive cleanup.
                "archive_checkpoints_removed": True,
            },
        )

        with (
            patch.object(
                tickets.runtime_state,
                "list_records",
                return_value=[record],
            ),
            patch.object(
                tickets,
                "_clean_completed_archive_record",
                new=AsyncMock(return_value=True),
            ) as clean_archive,
        ):
            await tickets.reconcile_tickets(bot)

        clean_archive.assert_awaited_once_with(guild, record, 42)

    async def test_restart_cleans_opening_checkpoints_from_retained_closed_ticket(
        self,
    ) -> None:
        source_channel = SimpleNamespace(id=505)
        guild = SimpleNamespace(id=707, text_channels=[])
        bot = SimpleNamespace(user=SimpleNamespace(id=42), guilds=[guild])
        record = SimpleNamespace(
            guild_id=707,
            external_id="505",
            status="closed",
            payload={"creation_id": "operation-one"},
        )

        with (
            patch.object(
                tickets,
                "reconcile_ticket_panel_publications",
                new=AsyncMock(),
            ),
            patch.object(tickets, "reconcile_ticket_creations", new=AsyncMock()),
            patch.object(tickets.runtime_state, "list_records", return_value=[record]),
            patch.object(
                tickets,
                "_fetch_guild_channel",
                new=AsyncMock(return_value=source_channel),
            ),
            patch.object(
                tickets,
                "_clean_active_ticket_creation_checkpoints",
                new=AsyncMock(return_value=True),
            ) as clean_creation,
            patch.object(tickets.runtime_state, "set_status") as set_status,
        ):
            await tickets.reconcile_tickets(bot)

        clean_creation.assert_awaited_once_with(source_channel, record)
        set_status.assert_not_called()

    async def test_restart_closes_record_when_completed_archive_lost_source(self) -> None:
        class FakeNotFound(Exception):
            pass

        guild = SimpleNamespace(id=707, text_channels=[])
        bot = SimpleNamespace(user=SimpleNamespace(id=42), guilds=[guild])
        record = SimpleNamespace(
            external_id="505",
            status="archiving",
            payload={"panel_id": "panel-a", "opener_id": "404"},
        )

        with (
            patch.object(tickets.discord, "NotFound", FakeNotFound),
            patch.object(
                tickets.runtime_state,
                "list_records",
                return_value=[record],
            ),
            patch.object(
                tickets,
                "_fetch_guild_channel",
                new=AsyncMock(side_effect=FakeNotFound()),
            ),
            patch.object(
                tickets,
                "_archive_is_complete_without_source",
                new=AsyncMock(return_value=True),
            ) as archive_complete,
            patch.object(tickets.runtime_state, "set_status") as set_status,
        ):
            await tickets.reconcile_tickets(bot)

        archive_complete.assert_awaited_once_with(guild, record, 42)
        set_status.assert_called_once_with(
            tickets._TICKET_RUNTIME_KIND,
            707,
            "505",
            "closed",
        )

    async def test_restart_retries_deletion_after_archive_completed(self) -> None:
        source_channel = SimpleNamespace(id=505)
        guild = SimpleNamespace(id=707, text_channels=[])
        bot = SimpleNamespace(user=SimpleNamespace(id=42), guilds=[guild])
        record = SimpleNamespace(
            external_id="505",
            status="archived_source_remaining",
            payload={"panel_id": "panel-a", "opener_id": "404"},
        )

        with (
            patch.object(
                tickets.runtime_state,
                "list_records",
                return_value=[record],
            ),
            patch.object(
                tickets,
                "_fetch_guild_channel",
                new=AsyncMock(return_value=source_channel),
            ),
            patch.object(
                tickets,
                "_delete_archived_ticket_source",
                new=AsyncMock(return_value=True),
            ) as delete_source,
        ):
            await tickets.reconcile_tickets(bot)

        delete_source.assert_awaited_once_with(
            source_channel,
            {
                "panel_id": "panel-a",
                "opener_id": "404",
                "opener_slug": "",
                "character": "",
                "albion_id": "",
            },
        )


if __name__ == "__main__":
    unittest.main()
