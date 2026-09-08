import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from src.realm_protector.bot import trial_configuration, trials
from src.realm_protector.infrastructure import runtime_state, sqlite_database, trial_store


class TrialConfigurationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        context = sqlite_database.database_path(Path(directory.name) / "trials.sqlite3")
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.values = {
            "category_id": 10,
            "role_id": 20,
            "manager_role_ids": [30, 31],
            "archive_channel_id": 40,
            "title": "Welcome",
            "message": "Your trial starts here.",
            "panel_channel_id": 60,
            "message_id": 100,
            "publication_id": "publication",
        }
        runtime_state.upsert_record(trial_store.CONFIG, 1, "main", self.values)
        self.adapter = trial_configuration.ADAPTER

    def trial(self, member_id=2, *, status="active", guild_id=1):
        record = trial_store.begin(guild_id, member_id, "AlbionName", self.values)
        return trial_store.save(
            record, status=status, channel_id=member_id + 200, message_id=member_id + 300
        )

    def stored(self, record):
        return runtime_state.get_record(record.kind, record.guild_id, record.external_id)

    def guild(self):
        channels = {}

        def channel_for(channel_id):
            if channel_id not in channels:
                message = SimpleNamespace(edit=AsyncMock())
                channels[channel_id] = MagicMock(
                    spec=discord.TextChannel,
                    id=channel_id,
                    fetch_message=AsyncMock(return_value=message),
                    send=AsyncMock(),
                )
            return channels[channel_id]

        guild = SimpleNamespace(
            id=1,
            fetch_channel=AsyncMock(side_effect=channel_for),
            fetch_member=AsyncMock(),
            create_text_channel=AsyncMock(),
        )
        return guild, channel_for

    def test_all_setup_fields_are_editable_and_message_binding_is_guild_scoped(self):
        self.assertEqual(
            {
                "category_id",
                "role_id",
                "manager_role_ids",
                "archive_channel_id",
                "title",
                "message",
            },
            {field.key for field in self.adapter.fields},
        )
        self.assertEqual(["main"], self.adapter.keys(1))
        self.assertEqual([], self.adapter.keys(2))
        self.assertEqual("main", self.adapter.resolve_key(1, 100))
        self.assertIsNone(self.adapter.resolve_key(2, 100))
        self.assertIsNone(self.adapter.resolve_key(1, 101))
        self.assertIsNone(self.adapter.load(1, "another"))

    def test_save_keeps_lifecycle_snapshots_and_updates_only_open_trial_text(self):
        active = self.trial()
        creating = self.trial(3, status="creating")
        ending = self.trial(4, status="ending")
        removing_role = self.trial(5, status="removing_role")
        closed = self.trial(6, status="closed")
        other_guild = self.trial(guild_id=2)
        changed = {
            **self.values,
            "category_id": 11,
            "role_id": 21,
            "manager_role_ids": [32],
            "archive_channel_id": 41,
            "title": "Updated trial",
            "message": "Updated instructions.",
        }
        self.adapter.save(1, "main", changed)
        configuration = trial_store.config(1).payload
        self.assertEqual(changed, configuration)
        for record in (active, creating):
            saved = self.stored(record)
            self.assertEqual(record.status, saved.status)
            self.assertEqual(record.payload["nickname"], saved.payload["nickname"])
            self.assertEqual(record.payload["member_id"], saved.payload["member_id"])
            self.assertEqual(record.payload["channel_id"], saved.payload["channel_id"])
            self.assertEqual(record.payload["message_id"], saved.payload["message_id"])
            self.assertEqual(
                {**self.values, "title": changed["title"], "message": changed["message"]},
                saved.payload["config"],
            )
        for record in (ending, removing_role, closed, other_guild):
            self.assertEqual(record, self.stored(record))

    def test_save_preserves_publication_metadata_instead_of_accepting_editable_overrides(self):
        self.adapter.save(1, "main", {**self.values, "message_id": 999, "title": "New"})
        saved = trial_store.config(1).payload
        self.assertEqual(100, saved["message_id"])
        self.assertEqual(60, saved["panel_channel_id"])
        self.assertEqual("publication", saved["publication_id"])
        self.assertEqual("New", saved["title"])

    def test_config_and_trial_snapshot_changes_roll_back_together(self):
        record = self.trial()
        save = runtime_state.upsert_record_in_transaction

        def fail_trial(database, kind, *args, **kwargs):
            if kind == trial_store.TRIAL:
                raise RuntimeError("storage failure")
            return save(database, kind, *args, **kwargs)

        with patch.object(runtime_state, "upsert_record_in_transaction", side_effect=fail_trial):
            with self.assertRaisesRegex(RuntimeError, "storage failure"):
                self.adapter.save(1, "main", {**self.values, "title": "New"})
        self.assertEqual(self.values, trial_store.config(1).payload)
        self.assertEqual(record, self.stored(record))

    async def test_validate_reuses_existing_role_privacy_and_permission_checks(self):
        guild = SimpleNamespace(id=1)
        with patch.object(trials, "validate_configuration") as validate:
            await self.adapter.validate(guild, "main", self.values)
            validate.assert_called_once_with(guild, self.values)
        with patch.object(trials, "validate_configuration", side_effect=ValueError("private")):
            with self.assertRaisesRegex(ValueError, "private"):
                await self.adapter.validate(guild, "main", self.values)

    async def test_validate_rejects_empty_or_oversized_text_and_removed_config(self):
        guild = SimpleNamespace(id=1)
        with patch.object(trials, "validate_configuration") as validate:
            for field, value in (("title", " "), ("title", "T" * 257), ("message", "M" * 4001)):
                with self.subTest(field=field, length=len(value)):
                    with self.assertRaises(ValueError):
                        await self.adapter.validate(guild, "main", {**self.values, field: value})
            validate.assert_not_called()
        runtime_state.delete_record(trial_store.CONFIG, 1, "main")
        with self.assertRaisesRegex(ValueError, "no longer exists"):
            await self.adapter.validate(guild, "main", self.values)
        with self.assertRaisesRegex(ValueError, "no longer exists"):
            self.adapter.save(1, "main", self.values)

    async def test_refresh_edits_recorded_panels_preserving_player_and_end_trial_control(self):
        active = self.trial()
        closed = self.trial(3, status="closed")
        self.adapter.save(1, "main", {**self.values, "title": "Changed", "message": "New text"})
        guild, channel_for = self.guild()
        with patch.object(trials, "create_trial", new=AsyncMock()) as create:
            self.assertEqual([], await self.adapter.refresh(guild, "main"))
            create.assert_not_awaited()
        guild.fetch_member.assert_not_awaited()
        guild.create_text_channel.assert_not_awaited()
        self.assertEqual(
            {60, active.payload["channel_id"]},
            {call.args[0] for call in guild.fetch_channel.await_args_list},
        )
        panel = channel_for(60).fetch_message.return_value
        self.assertTrue(panel.edit.await_args.kwargs["view"].is_persistent())
        trial_panel = channel_for(active.payload["channel_id"]).fetch_message.return_value
        kwargs = trial_panel.edit.await_args.kwargs
        self.assertEqual("Changed", kwargs["embed"].title)
        self.assertEqual("New text", kwargs["embed"].description)
        self.assertEqual("Player", kwargs["embed"].fields[0].name)
        self.assertEqual("<@2> • AlbionName", kwargs["embed"].fields[0].value)
        self.assertEqual(["End Trial"], [item.label for item in kwargs["view"].children])
        self.assertEqual(
            discord.AllowedMentions.none().to_dict(), kwargs["allowed_mentions"].to_dict()
        )
        channel_for(60).send.assert_not_awaited()
        channel_for(active.payload["channel_id"]).send.assert_not_awaited()
        self.assertEqual(closed, self.stored(closed))

    async def test_refresh_reports_deleted_panel_and_continues_without_reposting(self):
        active = self.trial()
        guild, channel_for = self.guild()
        channel_for(60).fetch_message.side_effect = discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"), "gone"
        )
        warnings = await self.adapter.refresh(guild, "main")
        self.assertEqual(1, len(warnings))
        self.assertIn("deleted", warnings[0])
        self.assertFalse(warnings.retryable)
        channel_for(60).send.assert_not_awaited()
        channel_for(
            active.payload["channel_id"]
        ).fetch_message.return_value.edit.assert_awaited_once()

    async def test_refresh_failure_can_retry_without_recreating_or_reverting_snapshots(self):
        active = self.trial()
        self.adapter.save(1, "main", {**self.values, "title": "Changed"})
        guild, channel_for = self.guild()
        panel = channel_for(active.payload["channel_id"]).fetch_message.return_value
        panel.edit.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "permissions"
        )
        with patch.object(trial_configuration.LOGGER, "exception"):
            warnings = await self.adapter.refresh(guild, "main")
        self.assertEqual(1, len(warnings))
        self.assertIn("retry", warnings[0])
        self.assertTrue(warnings.retryable)
        self.assertEqual("Changed", self.stored(active).payload["config"]["title"])
        panel.edit.side_effect = None
        self.assertEqual([], await self.adapter.refresh(guild, "main"))
        self.assertEqual("Changed", panel.edit.await_args.kwargs["embed"].title)
        channel_for(active.payload["channel_id"]).send.assert_not_awaited()

    async def test_pending_creation_without_a_message_is_left_for_lifecycle_recovery(self):
        pending = trial_store.begin(1, 2, "AlbionName", self.values)
        self.adapter.save(1, "main", {**self.values, "message": "Changed"})
        guild, _ = self.guild()
        self.assertEqual([], await self.adapter.refresh(guild, "main"))
        guild.fetch_channel.assert_awaited_once_with(60)
        self.assertEqual("creating", self.stored(pending).status)
        self.assertEqual("Changed", self.stored(pending).payload["config"]["message"])

    async def test_configuration_publication_includes_persistent_update_controls(self):
        guild, _ = self.guild()
        record = trial_store.config(1)
        with patch.object(trials, "publish_message", new=AsyncMock(return_value=record)) as publish:
            await trials.publish_configuration(guild, record)
        view = publish.await_args.kwargs["view"]
        self.assertTrue(view.is_persistent())
        self.assertEqual(1, len(view.children))
        self.assertEqual("Update Config", view.children[0].item.label)

    async def test_disabled_configuration_cannot_be_edited_or_start_new_trials(self):
        active = self.trial()
        runtime_state.set_status(trial_store.CONFIG, 1, "main", "disabled")
        guild, _ = self.guild()
        self.assertIsNone(self.adapter.load(1, "main"))
        self.assertEqual([], self.adapter.keys(1))
        self.assertIsNone(self.adapter.resolve_key(1, 100))
        with self.assertRaisesRegex(ValueError, "no longer exists"):
            await self.adapter.validate(guild, "main", self.values)
        with self.assertRaisesRegex(ValueError, "no longer exists"):
            self.adapter.save(1, "main", self.values)
        self.assertEqual([], await self.adapter.refresh(guild, "main"))
        guild.fetch_channel.assert_not_awaited()
        interaction = SimpleNamespace(
            guild=guild,
            user=SimpleNamespace(guild_permissions=SimpleNamespace(administrator=True)),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        with patch.object(trials, "create_trial", new=AsyncMock()) as create:
            await trials.handle_trial_add(interaction, SimpleNamespace(id=3))
            create.assert_not_awaited()
        self.assertIn("/trial-setup first", interaction.followup.send.await_args.args[0])
        self.assertEqual([active], runtime_state.list_records(trial_store.TRIAL, guild_id=1))
        self.assertTrue(trials.can_manage(interaction.user, active.payload["config"]))

    def test_publishing_configuration_is_editable_but_other_retired_states_are_not(self):
        runtime_state.set_status(trial_store.CONFIG, 1, "main", "publishing")
        self.assertEqual(self.values, self.adapter.load(1, "main"))
        self.assertEqual(["main"], self.adapter.keys(1))
        self.assertEqual("main", self.adapter.resolve_key(1, 100))
        for status in ("disabled", "removed", "closed"):
            runtime_state.set_status(trial_store.CONFIG, 1, "main", status)
            self.assertIsNone(self.adapter.load(1, "main"))
            self.assertEqual([], self.adapter.keys(1))

    async def test_mixed_terminal_and_transient_failures_retry_only_until_transient_resolves(self):
        active = self.trial()
        guild, channel_for = self.guild()
        channel_for(60).fetch_message.side_effect = discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"), "gone"
        )
        panel = channel_for(active.payload["channel_id"]).fetch_message.return_value
        panel.edit.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "permissions"
        )
        with patch.object(trial_configuration.LOGGER, "exception"):
            warnings = await self.adapter.refresh(guild, "main")
        self.assertEqual(2, len(warnings))
        self.assertTrue(warnings.retryable)
        panel.edit.side_effect = None
        warnings = await self.adapter.refresh(guild, "main")
        self.assertEqual(1, len(warnings))
        self.assertFalse(warnings.retryable)

    async def test_missing_recorded_location_is_terminal_without_reposting(self):
        record = trial_store.config(1)
        trial_store.save(record, message_id=None)
        active = self.trial()
        trial_store.save(active, message_id=None)
        guild, _ = self.guild()
        warnings = await self.adapter.refresh(guild, "main")
        self.assertEqual(2, len(warnings))
        self.assertTrue(all("no recorded message location" in warning for warning in warnings))
        self.assertFalse(warnings.retryable)
        guild.fetch_channel.assert_not_awaited()

    async def test_refresh_skips_current_messages_even_with_generated_component_ids(self):
        active = self.trial()
        guild, channel_for = self.guild()
        for channel_id, embed, view in (
            (
                60,
                trials.configuration_embed(self.values),
                trial_configuration.ConfigActionsView("trial", "main"),
            ),
            (active.payload["channel_id"], trials.trial_embed(active.payload), trials.TrialView()),
        ):
            message = channel_for(channel_id).fetch_message.return_value
            message.embeds = [embed]
            payloads = view.to_components()
            for payload in payloads:
                payload["id"] = 999
                for component in payload["components"]:
                    component["id"] = 1000
            message.components = [discord.ActionRow(payload) for payload in payloads]
        self.assertEqual([], await self.adapter.refresh(guild, "main"))
        channel_for(60).fetch_message.return_value.edit.assert_not_awaited()
        channel_for(
            active.payload["channel_id"]
        ).fetch_message.return_value.edit.assert_not_awaited()

    async def test_matching_embed_does_not_skip_backfilling_missing_update_controls(self):
        guild, channel_for = self.guild()
        message = channel_for(60).fetch_message.return_value
        message.embeds = [trials.configuration_embed(self.values)]
        message.components = []
        self.assertEqual([], await self.adapter.refresh(guild, "main"))
        message.edit.assert_awaited_once()
