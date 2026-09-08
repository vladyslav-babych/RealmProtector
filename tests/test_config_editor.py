import copy
import re
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from src.realm_protector.bot import config_editor
from src.realm_protector.infrastructure import document_store, runtime_state, sqlite_database


class MemoryAdapter:
    kind = "test"
    title = "Test Configuration"
    fields = (
        config_editor.ConfigField("title", "Panel title", max_length=256),
        config_editor.ConfigField("message", "Panel message"),
        config_editor.ConfigField("roles", "Managers", kind="roles"),
    )

    def __init__(self):
        self.values = {"title": "Old title", "message": "Old message", "roles": [100]}
        self.validate = AsyncMock()
        self.refresh = AsyncMock(return_value=[])
        self.save_calls = []

    def load(self, guild_id, key):
        return copy.deepcopy(self.values) if guild_id == 123 and key == "main" else None

    def save(self, guild_id, key, values):
        queued = runtime_state.get_record(config_editor.REFRESH_KIND, guild_id, "test:main")
        assert queued is not None and queued.status == "pending"
        self.save_calls.append(copy.deepcopy(values))
        self.values = copy.deepcopy(values)

    def keys(self, guild_id):
        return ["main"]

    def resolve_key(self, guild_id, message_id):
        return "main" if message_id == 456 else None


class ConfigEditorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = sqlite_database.database_path(Path(self.directory.name) / "test.sqlite3")
        self.database.__enter__()
        self.addCleanup(self.database.__exit__, None, None, None)
        self.adapter = MemoryAdapter()
        self.adapter_patch = patch.object(config_editor, "get_adapter", return_value=self.adapter)
        self.adapter_patch.start()
        self.addCleanup(self.adapter_patch.stop)
        self.target_patch = patch.object(
            config_editor.guild_settings, "get_target_guild", return_value="Kingsblood"
        )
        self.target_patch.start()
        self.addCleanup(self.target_patch.stop)
        self.guild = SimpleNamespace(id=123, get_role=lambda role_id: SimpleNamespace(id=role_id))
        self.member = MagicMock(spec=discord.Member)
        self.member.id = 321
        self.member.guild_permissions = discord.Permissions(administrator=True)
        self.interaction = SimpleNamespace(
            guild=self.guild,
            user=self.member,
            message=SimpleNamespace(id=456),
            response=SimpleNamespace(
                is_done=MagicMock(return_value=False),
                defer=AsyncMock(),
                send_message=AsyncMock(),
                send_modal=AsyncMock(),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
            edit_original_response=AsyncMock(return_value=SimpleNamespace(id=456)),
        )

    def draft(self, **updates):
        return runtime_state.upsert_record(
            config_editor.DRAFT_KIND,
            123,
            "a" * 32,
            {
                "kind": "test",
                "key": "main",
                "user_id": 321,
                "message_id": 456,
                "expires_at": time.time() + 900,
                "revision": 0,
                "target_guild": "Kingsblood",
                "selected": "title",
                "original": "Old title",
                "pending": "New title",
                **updates,
            },
        )

    async def test_open_is_private_and_does_not_change_live_values(self):
        await config_editor.open_editor(self.interaction, "test")
        self.interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        self.assertEqual([], self.adapter.save_calls)
        self.adapter.refresh.assert_not_awaited()
        drafts = runtime_state.list_records(config_editor.DRAFT_KIND, statuses=("active",))
        self.assertEqual(1, len(drafts))
        self.assertEqual(456, drafts[0].payload["message_id"])
        view = self.interaction.edit_original_response.await_args.kwargs["view"]
        self.assertTrue(view.is_persistent())

    async def test_non_admin_cannot_open_editor(self):
        self.member.guild_permissions = discord.Permissions.none()
        await config_editor.open_editor(self.interaction, "test")
        self.interaction.response.send_message.assert_awaited_once()
        self.assertTrue(self.interaction.response.send_message.await_args.kwargs["ephemeral"])
        self.assertEqual([], runtime_state.list_records(config_editor.DRAFT_KIND))

    async def test_current_button_rejects_replaced_public_panel(self):
        self.interaction.message.id = 999
        await config_editor.open_editor(self.interaction, "test", "current")
        self.assertEqual([], runtime_state.list_records(config_editor.DRAFT_KIND))

    def test_draft_rejects_other_user_server_and_message(self):
        record = self.draft()
        self.member.id = 654
        with self.assertRaisesRegex(ValueError, "administrator who opened"):
            config_editor.read_draft(self.interaction, record.external_id)
        self.member.id = 321
        self.interaction.message.id = 999
        with self.assertRaisesRegex(ValueError, "outdated editor"):
            config_editor.read_draft(self.interaction, record.external_id)
        self.interaction.guild = SimpleNamespace(id=999)
        with self.assertRaisesRegex(ValueError, "editor has ended"):
            config_editor.read_draft(self.interaction, record.external_id)

    def test_expired_draft_is_disabled(self):
        record = self.draft(expires_at=time.time() - 1)
        with self.assertRaisesRegex(ValueError, "expired"):
            config_editor.read_draft(self.interaction, record.external_id)
        self.assertEqual(
            "expired",
            runtime_state.get_record(config_editor.DRAFT_KIND, 123, record.external_id).status,
        )

    async def test_selector_loads_current_and_preview_without_saving(self):
        record = self.draft()
        await config_editor.ConfigEditorView(record, self.guild).act(
            self.interaction, "field", ["message"]
        )
        updated = runtime_state.get_record(config_editor.DRAFT_KIND, 123, record.external_id)
        self.assertEqual("message", updated.payload["selected"])
        self.assertEqual("Old message", updated.payload["original"])
        self.assertEqual("Old message", updated.payload["pending"])
        self.assertEqual([], self.adapter.save_calls)

    async def test_changed_setting_is_saved_before_discord_refresh(self):
        record = self.draft()

        async def refresh(guild, key):
            self.assertEqual("New title", self.adapter.values["title"])
            return []

        self.adapter.refresh.side_effect = refresh
        updated = await config_editor.save_change(
            self.interaction, record, self.adapter.fields[0], 1
        )
        self.assertEqual("New title", updated.payload["original"])
        self.assertEqual(
            "completed",
            runtime_state.get_record(config_editor.REFRESH_KIND, 123, "test:main").status,
        )

    async def test_same_field_conflict_is_rejected(self):
        record = self.draft()
        self.adapter.values["title"] = "Other administrator's title"
        with self.assertRaisesRegex(ValueError, "Another administrator"):
            await config_editor.save_change(self.interaction, record, self.adapter.fields[0], 1)
        self.assertEqual([], self.adapter.save_calls)
        self.adapter.refresh.assert_not_awaited()

    async def test_unrelated_concurrent_changes_are_preserved(self):
        record = self.draft()
        self.adapter.values["message"] = "Other administrator's message"
        await config_editor.save_change(self.interaction, record, self.adapter.fields[0], 1)
        self.assertEqual("New title", self.adapter.values["title"])
        self.assertEqual("Other administrator's message", self.adapter.values["message"])

    async def test_admin_revoked_during_validation_cannot_commit(self):
        async def revoke(guild, key, values):
            self.member.guild_permissions = discord.Permissions.none()

        self.adapter.validate.side_effect = revoke
        with self.assertRaisesRegex(ValueError, "administrators"):
            await config_editor.save_change(
                self.interaction, self.draft(), self.adapter.fields[0], 1
            )
        self.assertEqual([], self.adapter.save_calls)

    async def test_failed_projection_preserves_saved_values_and_retry_intent(self):
        self.adapter.refresh.return_value = ["Missing channel permissions"]
        updated = await config_editor.save_change(
            self.interaction, self.draft(), self.adapter.fields[0], 1
        )
        self.assertEqual("New title", self.adapter.values["title"])
        self.assertIn("retry automatically", updated.payload["status_message"])
        self.assertEqual(
            "pending", runtime_state.get_record(config_editor.REFRESH_KIND, 123, "test:main").status
        )
        self.adapter.refresh.return_value = []
        await config_editor.refresh_configuration(self.guild, "test", "main")
        self.assertEqual(
            "completed",
            runtime_state.get_record(config_editor.REFRESH_KIND, 123, "test:main").status,
        )

    async def test_cancel_discards_unsaved_preview(self):
        record = self.draft()
        await config_editor.ConfigEditorView(record, self.guild).act(self.interaction, "cancel")
        self.assertEqual("Old title", self.adapter.values["title"])
        self.assertEqual(
            "closed",
            runtime_state.get_record(config_editor.DRAFT_KIND, 123, record.external_id).status,
        )
        self.assertIsNone(self.interaction.edit_original_response.await_args.kwargs["view"])

    async def test_stale_modal_cannot_write_into_different_field(self):
        record = self.draft()
        modal = config_editor.ConfigValueModal(record)
        config_editor._save_draft(record, selected="message", revision=1)
        await modal.on_submit(self.interaction)
        saved = runtime_state.get_record(config_editor.DRAFT_KIND, 123, record.external_id)
        self.assertEqual("New title", saved.payload["pending"])
        self.assertEqual([], self.adapter.save_calls)
        self.interaction.response.send_message.assert_awaited_once()

    def test_long_current_and_new_text_stay_inside_discord_limits(self):
        self.adapter.values["message"] = "a" * 4000
        record = self.draft(selected="message", original="a" * 4000, pending="b" * 4000)
        embed = config_editor.editor_embed(record)
        self.assertLessEqual(len(embed), 6000)
        self.assertTrue(all(len(field.value) <= 1024 for field in embed.fields))
        self.assertIn("Preview shortened", "".join(field.value for field in embed.fields))

    def test_restart_restores_unexpired_private_view(self):
        record = self.draft()
        bot = MagicMock()
        config_editor.register_persistent_views(bot)
        bot.add_dynamic_items.assert_called_once_with(config_editor.UpdateConfigButton)
        bot.add_view.assert_called_once()
        view = bot.add_view.call_args.args[0]
        self.assertEqual(record.external_id, view.draft_id)
        self.assertEqual(456, bot.add_view.call_args.kwargs["message_id"])
        self.assertTrue(view.is_persistent())

    async def test_open_text_modal_can_be_submitted_after_restart(self):
        record = self.draft()
        await config_editor.ConfigEditorView(record, self.guild).act(self.interaction, "edit")
        original_modal = self.interaction.response.send_modal.await_args.args[0]
        bot = MagicMock()
        config_editor.register_persistent_views(bot)
        restored_modal = bot._connection.store_view.call_args.args[0]
        self.assertEqual(original_modal.custom_id, restored_modal.custom_id)
        self.assertEqual(original_modal.value_input.custom_id, restored_modal.value_input.custom_id)
        restored_modal.value_input._value = "Draft text after restart"
        await restored_modal.on_submit(self.interaction)
        current = runtime_state.get_record(config_editor.DRAFT_KIND, 123, record.external_id)
        self.assertEqual("Draft text after restart", current.payload["pending"])
        self.assertEqual("Old title", self.adapter.values["title"])

    async def test_old_modal_is_rejected_after_restart_if_field_changed(self):
        record = self.draft()
        await config_editor.ConfigEditorView(record, self.guild).act(self.interaction, "edit")
        record = runtime_state.get_record(config_editor.DRAFT_KIND, 123, record.external_id)
        config_editor._save_draft(record, selected="message", revision=1)
        bot = MagicMock()
        config_editor.register_persistent_views(bot)
        restored_modal = bot._connection.store_view.call_args.args[0]
        await restored_modal.on_submit(self.interaction)
        self.interaction.response.send_message.assert_awaited_once()
        self.assertEqual([], self.adapter.save_calls)

    async def test_terminal_missing_panel_notice_does_not_retry_forever(self):
        self.adapter.refresh.return_value = config_editor.RefreshWarnings(
            ["Panel was deleted"], retryable=False
        )
        updated = await config_editor.save_change(
            self.interaction, self.draft(), self.adapter.fields[0], 1
        )
        self.assertIn("administrator attention", updated.payload["status_message"])
        self.assertEqual(
            "completed",
            runtime_state.get_record(config_editor.REFRESH_KIND, 123, "test:main").status,
        )

    async def test_validation_await_cannot_overwrite_a_newer_setup(self):
        async def change(guild, key, values):
            self.adapter.values["message"] = "Newer setup"

        self.adapter.validate.side_effect = change
        with self.assertRaisesRegex(ValueError, "changed during validation"):
            await config_editor.save_change(
                self.interaction, self.draft(), self.adapter.fields[0], 1
            )
        self.assertEqual([], self.adapter.save_calls)

    async def test_removed_server_cannot_open_even_retained_feature_configuration(self):
        with patch.object(config_editor.guild_settings, "get_target_guild", return_value=None):
            await config_editor.open_editor(self.interaction, "test")
        self.assertEqual([], runtime_state.list_records(config_editor.DRAFT_KIND))

    async def test_removed_server_pending_refreshes_are_cancelled(self):
        config_editor.queue_refresh(123, "test", "main")
        with patch.object(config_editor.guild_settings, "get_target_guild", return_value=None):
            await config_editor.reconcile_configuration_updates(
                SimpleNamespace(guilds=[self.guild])
            )
        self.assertEqual(
            "cancelled",
            runtime_state.get_record(config_editor.REFRESH_KIND, 123, "test:main").status,
        )
        self.adapter.refresh.assert_not_awaited()

    async def test_expired_private_views_are_released_without_restart(self):
        record = self.draft(expires_at=time.time() - 1)
        view = config_editor.ConfigEditorView(record, self.guild)
        await config_editor.reconcile_configuration_updates(
            SimpleNamespace(guilds=[], persistent_views=[view])
        )
        self.assertTrue(view.is_finished())
        self.assertEqual(
            "expired",
            runtime_state.get_record(config_editor.DRAFT_KIND, 123, record.external_id).status,
        )

    async def test_dynamic_button_restores_exact_target(self):
        custom_id = "realm:config:update:ticket:abcdef123"
        match = re.fullmatch(
            config_editor.UpdateConfigButton.__discord_ui_compiled_template__, custom_id
        )
        restored = await config_editor.UpdateConfigButton.from_custom_id(
            self.interaction, None, match
        )
        self.assertEqual("ticket", restored.kind)
        self.assertEqual("abcdef123", restored.key)

    async def test_empty_and_oversized_values_are_rejected(self):
        for value in ("", "x" * 257):
            with self.subTest(value_length=len(value)):
                with self.assertRaises(ValueError):
                    await config_editor.save_change(
                        self.interaction, self.draft(pending=value), self.adapter.fields[0], 1
                    )
        self.assertEqual([], self.adapter.save_calls)


class BotConfigurationAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_bot_update_preserves_other_roles_and_channel(self):
        from src.realm_protector.bot import bot_configuration

        with (
            TemporaryDirectory() as directory,
            sqlite_database.database_path(Path(directory) / "state.sqlite3"),
        ):
            document_store.upsert_mapping_entry(
                "guild_settings",
                123,
                {
                    "guild_name": "Kingsblood",
                    "caller_role_ids": [101],
                    "caller_role_name": "Caller",
                    "member_role_id": 102,
                    "member_role_name": "Member",
                    "economy_manager_role_ids": [103],
                    "economy_manager_role_name": "Economy",
                    "bot_updates_channel_id": 105,
                    "bot_config_channel_id": 106,
                    "bot_config_message_id": 107,
                },
            )
            adapter = bot_configuration.ADAPTER
            values = adapter.load(123, "main")
            values["leave_action"] = "none"
            adapter.save(123, "main", values)
            updated = adapter.load(123, "main")
            self.assertEqual("none", updated["leave_action"])
            self.assertEqual([101], updated["caller_roles"])
            self.assertEqual(102, updated["member_role"])
            self.assertEqual(105, updated["bot_updates_channel_id"])
            self.assertEqual(
                (106, 107), bot_configuration.guild_settings.get_bot_configuration_message(123)
            )

    def test_every_bot_setup_point_is_editable(self):
        from src.realm_protector.bot import bot_configuration

        self.assertTrue(
            {
                "guild_name",
                "caller_roles",
                "economy_roles",
                "member_role",
                "leave_action",
                "bot_updates_channel_id",
            }
            <= {field.key for field in bot_configuration.ADAPTER.fields}
        )
