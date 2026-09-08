import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from src.realm_protector.bot import objective_configuration, objectives, reaction_roles
from src.realm_protector.bot import reaction_configuration as reactions_config
from src.realm_protector.infrastructure import guild_settings, local_repository, sqlite_database


class PanelConfigurationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_context = sqlite_database.database_path(
            Path(self.temporary_directory.name) / "panels.sqlite3"
        )
        self.database_context.__enter__()
        local_repository.ensure_schema()
        guild_settings.set_target_guild(42, "King's Blood")
        self.panel = {
            "id": "roles",
            "panel_name": "Choose roles",
            "panel_message": "React below",
            "destination_channel_id": 200,
            "panel_channel_id": 200,
            "panel_message_id": 300,
            "reactions": [{"emoji": "🔥", "role_id": 99}],
        }
        reaction_roles._save_panel(42, deepcopy(self.panel))
        reaction_roles._offline_reconciled_panel_versions.clear()

    def tearDown(self) -> None:
        self.database_context.__exit__(None, None, None)
        self.temporary_directory.cleanup()
        reaction_roles._offline_reconciled_panel_versions.clear()

    def channel(self, channel_id=200, message_id=300):
        channel = Mock(spec=discord.TextChannel)
        channel.id = channel_id
        channel.permissions_for.return_value = discord.Permissions.all()
        message = SimpleNamespace(
            id=message_id,
            channel=channel,
            edit=AsyncMock(),
            delete=AsyncMock(),
            clear_reactions=AsyncMock(),
            clear_reaction=AsyncMock(),
            add_reaction=AsyncMock(),
            reactions=[],
        )
        channel.fetch_message = AsyncMock(return_value=message)
        channel.send = AsyncMock(return_value=message)
        return channel, message

    def guild(self, channels, role=None):
        role = role or SimpleNamespace(id=99, mention="<@&99>", members=[])
        return SimpleNamespace(
            id=42,
            me=SimpleNamespace(id=1),
            get_channel=lambda channel_id: channels.get(channel_id),
            fetch_channel=AsyncMock(side_effect=lambda channel_id: channels[channel_id]),
            get_role=lambda role_id: role,
        )

    def test_text_update_preserves_mapping_runtime_state(self) -> None:
        self.panel["reactions"][0]["tracked_member_ids"] = [20, 30]
        reaction_roles._save_panel(42, self.panel)
        values = reactions_config.ADAPTER.load(42, "roles")
        assert values is not None
        self.assertNotIn("tracked_member_ids", values["reactions"][0])
        values["panel_message"] = "Updated instructions"

        reactions_config.ADAPTER.save(42, "roles", values)

        current = reaction_roles._get_panel_by_id(42, "roles")
        assert current is not None
        self.assertEqual([20, 30], current["reactions"][0]["tracked_member_ids"])
        self.assertEqual([], current["pending_reaction_resets"])
        self.assertEqual(300, current["panel_message_id"])

    def test_remapping_stages_reset_and_never_carries_previous_role_ownership(self) -> None:
        values = reactions_config.ADAPTER.load(42, "roles")
        assert values is not None
        values["reactions"] = [{"emoji": "🔥", "role_id": 100}, {"emoji": "✅", "role_id": 101}]

        reactions_config.ADAPTER.save(42, "roles", values)

        current = reaction_roles._get_panel_by_id(42, "roles")
        assert current is not None
        self.assertCountEqual(["🔥", "✅"], current["pending_reaction_resets"])
        self.assertTrue(all(item["tracked_member_ids"] == [] for item in current["reactions"]))

    async def test_validation_rejects_duplicate_emojis_and_inaccessible_channels(self) -> None:
        channel, _ = self.channel()
        guild = self.guild({200: channel})
        values = reactions_config.ADAPTER.load(42, "roles")
        assert values is not None
        values["reactions"] *= 2
        with (
            patch.object(
                reactions_config.role_security, "self_assignment_error", return_value=None
            ),
            self.assertRaisesRegex(ValueError, "only once"),
        ):
            await reactions_config.ADAPTER.validate(guild, "roles", values)
        values["reactions"] = values["reactions"][:1]
        channel.permissions_for.return_value = discord.Permissions.none()
        with (
            patch.object(
                reactions_config.role_security, "self_assignment_error", return_value=None
            ),
            self.assertRaisesRegex(ValueError, "Read Message History"),
        ):
            await reactions_config.ADAPTER.validate(guild, "roles", values)

    async def test_mapping_update_requires_permission_to_clear_old_reactions(self) -> None:
        channel, _ = self.channel()
        permissions = discord.Permissions.all()
        permissions.manage_messages = False
        channel.permissions_for.return_value = permissions
        guild = self.guild({200: channel})
        values = reactions_config.ADAPTER.load(42, "roles")
        assert values is not None
        values["reactions"][0]["role_id"] = 100
        with (
            patch.object(
                reactions_config.role_security, "self_assignment_error", return_value=None
            ),
            self.assertRaisesRegex(ValueError, "Manage Messages"),
        ):
            await reactions_config.ADAPTER.validate(guild, "roles", values)

    async def test_refresh_edits_current_panel_without_resetting_unchanged_reactions(self) -> None:
        channel, message = self.channel()
        guild = self.guild({200: channel})
        values = reactions_config.ADAPTER.load(42, "roles")
        assert values is not None
        values["panel_message"] = "New message"
        reactions_config.ADAPTER.save(42, "roles", values)

        warnings = await reactions_config.ADAPTER.refresh(guild, "roles")

        self.assertEqual([], warnings)
        channel.send.assert_not_awaited()
        message.clear_reaction.assert_not_awaited()
        kwargs = message.edit.await_args.kwargs
        self.assertIn("New message", kwargs["embed"].description)
        self.assertTrue(any(item.item.label == "Update Config" for item in kwargs["view"].children))

    async def test_deleted_reaction_message_is_not_reposted_by_refresh(self) -> None:
        channel, _ = self.channel()
        channel.fetch_message.side_effect = discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"), "Deleted"
        )

        warnings = await reactions_config.ADAPTER.refresh(self.guild({200: channel}), "roles")

        self.assertIn("deleted", warnings[0])
        self.assertFalse(warnings.retryable)
        channel.send.assert_not_awaited()
        self.assertEqual(300, reaction_roles._get_panel_by_id(42, "roles")["panel_message_id"])

    async def test_legacy_reaction_destination_falls_back_to_saved_panel_channel(self) -> None:
        self.panel.pop("destination_channel_id")
        reaction_roles._save_panel(42, self.panel)
        channel, message = self.channel()

        warnings = await reactions_config.ADAPTER.refresh(self.guild({200: channel}), "roles")

        self.assertEqual([], warnings)
        channel.send.assert_not_awaited()
        message.edit.assert_awaited_once()

    async def test_reaction_refresh_updates_tracked_configuration_summary(self) -> None:
        self.panel["configuration_channel_id"] = 202
        self.panel["configuration_message_id"] = 302
        reaction_roles._save_panel(42, self.panel)
        channel, _ = self.channel()
        summary_channel, summary = self.channel(202, 302)
        values = reactions_config.ADAPTER.load(42, "roles")
        assert values is not None
        values["panel_message"] = "Updated panel message"
        reactions_config.ADAPTER.save(42, "roles", values)

        warnings = await reactions_config.ADAPTER.refresh(
            self.guild({200: channel, 202: summary_channel}), "roles"
        )

        self.assertEqual([], warnings)
        self.assertEqual(
            "Updated panel message", summary.edit.await_args.kwargs["embed"].description
        )
        self.assertEqual("roles", reactions_config.ADAPTER.resolve_key(42, 302))
        self.assertEqual("current", summary.edit.await_args.kwargs["view"].children[0].key)

    async def test_reaction_summary_refresh_continues_when_live_panel_is_forbidden(self) -> None:
        self.panel["configuration_channel_id"] = 202
        self.panel["configuration_message_id"] = 302
        reaction_roles._save_panel(42, self.panel)
        channel, message = self.channel()
        summary_channel, summary = self.channel(202, 302)
        message.edit.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "Missing permission"
        )

        warnings = await reactions_config.ADAPTER.refresh(
            self.guild({200: channel, 202: summary_channel}), "roles"
        )

        self.assertTrue(warnings)
        summary.edit.assert_awaited_once()

    async def test_deleted_reaction_summary_is_a_terminal_refresh_warning(self) -> None:
        self.panel["configuration_channel_id"] = 202
        self.panel["configuration_message_id"] = 302
        reaction_roles._save_panel(42, self.panel)
        channel, _ = self.channel()
        summary_channel, _ = self.channel(202, 302)
        summary_channel.fetch_message.side_effect = discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"), "Deleted"
        )

        warnings = await reactions_config.ADAPTER.refresh(
            self.guild({200: channel, 202: summary_channel}), "roles"
        )

        self.assertFalse(warnings.retryable)
        self.assertIn("summary was deleted", warnings[0])
        summary_channel.send.assert_not_awaited()

    async def test_failed_reaction_reset_stays_durable_and_routing_is_inert(self) -> None:
        channel, message = self.channel()
        guild = self.guild({200: channel})
        values = reactions_config.ADAPTER.load(42, "roles")
        assert values is not None
        values["reactions"][0]["role_id"] = 100
        reactions_config.ADAPTER.save(42, "roles", values)
        message.clear_reaction.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "Missing permission"
        )

        warnings = await reactions_config.ADAPTER.refresh(guild, "roles")

        self.assertTrue(warnings)
        current = reaction_roles._get_panel_by_id(42, "roles")
        assert current is not None
        self.assertEqual(["🔥"], current["pending_reaction_resets"])
        member = SimpleNamespace(id=20, bot=False, add_roles=AsyncMock())
        guild.get_member = lambda _member_id: member
        payload = SimpleNamespace(guild_id=42, message_id=300, user_id=20, emoji="🔥")
        bot = SimpleNamespace(user=SimpleNamespace(id=1), get_guild=lambda _guild_id: guild)
        await reaction_roles.handle_raw_reaction_add(bot, payload)
        member.add_roles.assert_not_awaited()

        message.clear_reaction.side_effect = None
        self.assertEqual([], await reactions_config.ADAPTER.refresh(guild, "roles"))
        self.assertEqual(
            [], reaction_roles._get_panel_by_id(42, "roles")["pending_reaction_resets"]
        )

    async def test_offline_reconciliation_removes_only_owned_roles_for_changed_mappings(
        self,
    ) -> None:
        channel, _ = self.channel()
        self.panel["reactions"][0]["tracked_member_ids"] = [20]
        reaction_roles._save_panel(42, self.panel)
        tracked = SimpleNamespace(id=20, bot=False)
        existing = SimpleNamespace(id=21, bot=False)
        role = SimpleNamespace(id=99, mention="<@&99>", members=[tracked, existing])
        guild = self.guild({200: channel}, role)

        with (
            patch.object(reaction_roles.role_security, "self_assignment_error", return_value=None),
            patch.object(reaction_roles, "_apply_member_role_state", return_value=True) as apply,
        ):
            await reaction_roles._reconcile_reaction_assignments_for_guild(guild)

        apply.assert_awaited_once_with(tracked, role, desired=False)
        current = reaction_roles._get_panel_by_id(42, "roles")
        self.assertEqual([], current["reactions"][0]["tracked_member_ids"])

    async def test_live_reactions_record_ownership_after_mapping_change(self) -> None:
        self.panel["reactions"][0]["tracked_member_ids"] = []
        reaction_roles._save_panel(42, self.panel)
        guild = self.guild({})
        member = SimpleNamespace(id=20, bot=False, add_roles=AsyncMock(), remove_roles=AsyncMock())
        guild.get_member = lambda _member_id: member
        payload = SimpleNamespace(guild_id=42, message_id=300, user_id=20, emoji="🔥")
        bot = SimpleNamespace(user=SimpleNamespace(id=1), get_guild=lambda _guild_id: guild)

        with patch.object(reaction_roles.role_security, "self_assignment_error", return_value=None):
            await reaction_roles.handle_raw_reaction_add(bot, payload)
            current = reaction_roles._get_panel_by_id(42, "roles")
            self.assertEqual([20], current["reactions"][0]["tracked_member_ids"])
            await reaction_roles.handle_raw_reaction_remove(bot, payload)
        current = reaction_roles._get_panel_by_id(42, "roles")
        self.assertEqual([], current["reactions"][0]["tracked_member_ids"])

    async def test_reaction_destination_move_is_durable_and_not_repeated(self) -> None:
        old_channel, old_message = self.channel()
        new_channel, _ = self.channel(201, 301)
        guild = self.guild({200: old_channel, 201: new_channel})
        values = reactions_config.ADAPTER.load(42, "roles")
        assert values is not None
        values["destination_channel_id"] = 201
        reactions_config.ADAPTER.save(42, "roles", values)

        self.assertEqual([], await reactions_config.ADAPTER.refresh(guild, "roles"))
        self.assertEqual([], await reactions_config.ADAPTER.refresh(guild, "roles"))

        new_channel.send.assert_awaited_once()
        old_message.clear_reactions.assert_awaited_once()
        self.assertIsNone(old_message.edit.await_args.kwargs["view"])
        current = reaction_roles._get_panel_by_id(42, "roles")
        self.assertEqual(301, current["panel_message_id"])
        self.assertEqual([], current["reactions"][0]["tracked_member_ids"])
        self.assertIsNone(reactions_config.ADAPTER.resolve_key(42, 300))
        self.assertEqual("roles", reactions_config.ADAPTER.resolve_key(42, 301))

    def test_objective_presentation_save_preserves_live_timers_and_subscribers(self) -> None:
        objective = {"id": "one", "pop_at_ts": 9000, "notified_at": None, "subscriber_ids": [10]}
        objectives._save_guild_entry(
            42, {"panel_channel_id": 200, "panel_message_id": 300, "objectives": [objective]}
        )
        values = objective_configuration.ADAPTER.load(42, "current")
        assert values is not None
        values["panel_title"] = "Guild objectives"
        values["panel_message"] = "Add objectives below"
        # Model a scheduler/subscriber update while the private editor is open.
        objective["subscriber_ids"].append(11)
        objectives._update_objective(42, objective)

        objective_configuration.ADAPTER.save(42, "current", values)

        saved = objectives._load_guild_entry(42)
        self.assertEqual(objective, saved["objectives"][0])
        embed = objectives._build_panel_embed(SimpleNamespace(id=42))
        self.assertEqual("Guild objectives", embed.title)
        self.assertEqual("Add objectives below", embed.description)

    async def test_objective_refresh_preserves_add_button_and_other_messages(self) -> None:
        objectives._save_guild_entry(
            42,
            {"panel_channel_id": 200, "panel_message_id": 300, "objectives": [{"id": "one"}]},
        )
        channel, message = self.channel()
        guild = self.guild({200: channel})

        self.assertEqual([], await objective_configuration.ADAPTER.refresh(guild, "current"))

        channel.fetch_message.assert_awaited_once_with(300)
        view = message.edit.await_args.kwargs["view"]
        self.assertEqual("Add Objective", view.children[0].label)
        self.assertEqual("Update Config", view.children[1].item.label)
        self.assertEqual([{"id": "one"}], objectives._load_guild_entry(42)["objectives"])

    async def test_objective_destination_move_preserves_running_objectives(self) -> None:
        objective = {
            "id": "one",
            "channel_id": 200,
            "message_id": 305,
            "pop_at_ts": 9000,
            "subscriber_ids": [12],
        }
        objectives._save_guild_entry(
            42, {"panel_channel_id": 200, "panel_message_id": 300, "objectives": [objective]}
        )
        old_channel, old_message = self.channel()
        new_channel, new_message = self.channel(201, 301)
        new_message.content = ""
        new_message.embeds = []
        guild = self.guild({200: old_channel, 201: new_channel})
        values = objective_configuration.ADAPTER.load(42, "current")
        assert values is not None
        values["destination_channel_id"] = 201
        objective_configuration.ADAPTER.save(42, "current", values)

        self.assertEqual([], await objective_configuration.ADAPTER.refresh(guild, "current"))
        self.assertEqual([], await objective_configuration.ADAPTER.refresh(guild, "current"))

        new_channel.send.assert_awaited_once()
        old_message.delete.assert_awaited_once()
        entry = objectives._load_guild_entry(42)
        self.assertEqual("301", entry["panel_message_id"])
        self.assertEqual([objective], entry["objectives"])
        self.assertIsNone(objective_configuration.ADAPTER.resolve_key(42, 300))
        self.assertEqual("current", objective_configuration.ADAPTER.resolve_key(42, 301))

    async def test_deleted_objective_panel_is_not_reposted_by_refresh(self) -> None:
        objectives._save_guild_entry(
            42, {"panel_channel_id": 200, "panel_message_id": 300, "objectives": [{"id": "one"}]}
        )
        channel, _ = self.channel()
        channel.fetch_message.side_effect = discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"), "Deleted"
        )

        warnings = await objective_configuration.ADAPTER.refresh(
            self.guild({200: channel}), "current"
        )

        self.assertIn("deleted", warnings[0])
        self.assertFalse(warnings.retryable)
        channel.send.assert_not_awaited()
        self.assertEqual(300, objectives._load_guild_entry(42)["panel_message_id"])

    async def test_manager_view_update_button_targets_selected_panel(self) -> None:
        view = reaction_roles.ManagePanelsView(
            self.guild({}), 12, [self.panel], selected_id="roles"
        )
        update = next(child for child in view.children if hasattr(child, "kind"))
        self.assertEqual("reaction", update.kind)
        self.assertEqual("roles", update.key)

    def test_removed_configuration_cannot_be_resolved_by_old_panels(self) -> None:
        entry = reaction_roles._load_guild_entry(42)
        entry["disabled"] = True
        reaction_roles._save_guild_entry(42, entry)
        objectives._save_guild_entry(
            42, {"disabled": True, "panel_channel_id": 200, "panel_message_id": 300}
        )
        self.assertIsNone(reactions_config.ADAPTER.load(42, "roles"))
        self.assertIsNone(reactions_config.ADAPTER.resolve_key(42, 300))
        self.assertIsNone(objective_configuration.ADAPTER.load(42, "current"))
        self.assertIsNone(objective_configuration.ADAPTER.resolve_key(42, 300))


if __name__ == "__main__":
    unittest.main()
