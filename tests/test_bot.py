import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from src.realm_protector.bot.client import RealmProtectorBot

EXPECTED_SLASH_COMMANDS = {
    "add-utc-timer",
    "bal",
    "bal-add",
    "bal-remove",
    "bot-link-google-sheet",
    "bot-remove",
    "bot-setup",
    "clear",
    "get-negative-siphon",
    "get-participants",
    "force-register",
    "lootsplit",
    "register",
    "role-reaction-setup",
    "set-objective-panel",
    "sync-rebuild",
    "sync-retry",
    "sync-siphon",
    "sync-status",
    "tickets-setup",
    "update-config",
}

EXPECTED_PARAMETERS = {
    "add-utc-timer": [],
    "bal": [("member", False)],
    "bal-add": [("member", True), ("add_silver", True), ("reason", False)],
    "bal-remove": [("member", True), ("remove_silver", True), ("reason", False)],
    "bot-link-google-sheet": [],
    "bot-remove": [],
    "bot-setup": [],
    "clear": [],
    "get-negative-siphon": [],
    "get-participants": [("battle_ids", True)],
    "force-register": [("member", True)],
    "lootsplit": [
        ("battle_ids", True),
        ("content_name", True),
        ("caller", True),
        ("participants", True),
        ("lootsplit_amount", True),
        ("officer", False),
    ],
    "register": [("character_name", True)],
    "role-reaction-setup": [],
    "set-objective-panel": [],
    "sync-rebuild": [],
    "sync-retry": [],
    "sync-siphon": [],
    "sync-status": [],
    "tickets-setup": [],
    "update-config": [],
}


class BotCommandSurfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_stops_every_background_worker(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            bot = RealmProtectorBot(Path(temporary_directory))
            with (
                patch(
                    "src.realm_protector.bot.client.objectives.stop_objectives_scheduler",
                    new=AsyncMock(),
                ) as stop_objectives,
                patch(
                    "src.realm_protector.bot.client.membership.stop_guild_member_tracker",
                    new=AsyncMock(),
                ) as stop_membership,
                patch(
                    "src.realm_protector.bot.client.google_sync.stop_google_sync",
                    new=AsyncMock(),
                ) as stop_google,
                patch(
                    "src.realm_protector.bot.client.local_maintenance.stop_local_maintenance",
                    new=AsyncMock(),
                ) as stop_maintenance,
                patch(
                    "src.realm_protector.bot.client.utc_timer.stop_utc_timer_scheduler",
                    new=AsyncMock(),
                ) as stop_timer,
            ):
                await bot.close()

            stop_objectives.assert_awaited_once_with()
            stop_membership.assert_awaited_once_with()
            stop_google.assert_awaited_once_with()
            stop_maintenance.assert_awaited_once_with()
            stop_timer.assert_awaited_once_with()

    async def test_preserves_prefix_and_slash_command_surface(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            bot = RealmProtectorBot(Path(temporary_directory))
            try:
                self.assertIsNotNone(bot.get_command("create-comp"))
                self.assertIn(
                    "_post_housri_gif",
                    {listener.__name__ for listener in bot.extra_events.get("on_message", ())},
                )

                with (
                    patch(
                        "src.realm_protector.bot.client.tickets.register_persistent_views"
                    ) as register_ticket_views,
                    patch(
                        "src.realm_protector.bot.client.objectives.register_persistent_views"
                    ) as register_objective_views,
                    patch(
                        "src.realm_protector.bot.client.register_economy_persistent_views"
                    ) as register_economy_views,
                    patch.object(bot.tree, "sync", new=AsyncMock()) as sync,
                ):
                    await bot.setup_hook()
                    await bot.setup_hook()

                self.assertEqual(
                    EXPECTED_SLASH_COMMANDS,
                    {command.name for command in bot.tree.get_commands()},
                )
                self.assertEqual(21, len(bot.tree.get_commands()))
                self.assertEqual(
                    EXPECTED_PARAMETERS,
                    {
                        command.name: [
                            (parameter.name, parameter.required) for parameter in command.parameters
                        ]
                        for command in bot.tree.get_commands()
                    },
                )
                self.assertEqual(
                    "<comp_message_id> [source_channel_id]",
                    bot.get_command("create-comp").signature,
                )
                self.assertEqual("", bot.get_command("bal").signature)
                self.assertEqual("", bot.get_command("lb").signature)
                register_ticket_views.assert_called_with(bot)
                register_objective_views.assert_called_with(bot)
                register_economy_views.assert_called_with(bot)
                self.assertEqual(1, sync.await_count)
            finally:
                await bot.close()

    async def test_runtime_reconciliation_isolates_workflow_failures(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            bot = RealmProtectorBot(Path(temporary_directory))
            bot._legacy_roles_migrated = True
            bot._application_commands_synced = True
            bot._background_services_started = True
            bot._startup_notifications_sent = True
            failed_tickets = AsyncMock(side_effect=RuntimeError("ticket failure"))
            configuration_removals = AsyncMock()
            registrations = AsyncMock()
            configuration_panels = AsyncMock()
            compositions = AsyncMock()
            reaction_panels = AsyncMock()
            objective_actions = AsyncMock()
            try:
                with (
                    patch(
                        "src.realm_protector.bot.client.configuration_remove.reconcile_configuration_removals",
                        new=configuration_removals,
                    ),
                    patch(
                        "src.realm_protector.bot.client.registration.reconcile_registration_side_effects",
                        new=registrations,
                    ),
                    patch(
                        "src.realm_protector.bot.client.configuration_panel.reconcile_configuration_panels",
                        new=configuration_panels,
                    ),
                    patch(
                        "src.realm_protector.bot.client.tickets.reconcile_tickets",
                        new=failed_tickets,
                    ),
                    patch(
                        "src.realm_protector.bot.client.composition.reconcile_compositions",
                        new=compositions,
                    ),
                    patch(
                        "src.realm_protector.bot.client.reaction_roles.reconcile_reaction_role_panels",
                        new=reaction_panels,
                    ),
                    patch(
                        "src.realm_protector.bot.client.objectives.reconcile_objective_actions",
                        new=objective_actions,
                    ),
                    patch("src.realm_protector.bot.client.LOGGER.exception") as log_exception,
                ):
                    await bot.on_ready()

                configuration_removals.assert_awaited_once_with(bot)
                registrations.assert_awaited_once_with(bot)
                configuration_panels.assert_awaited_once_with(bot)
                failed_tickets.assert_awaited_once_with(bot)
                compositions.assert_awaited_once_with(bot)
                reaction_panels.assert_awaited_once_with(bot)
                objective_actions.assert_awaited_once_with(bot)
                self.assertFalse(bot._runtime_state_reconciled)
                log_exception.assert_called_once()
            finally:
                await bot.close()


if __name__ == "__main__":
    unittest.main()
