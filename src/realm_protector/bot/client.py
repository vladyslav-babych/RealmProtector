from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Iterable, Optional

import discord
from discord import app_commands
from discord.ext import commands

from src.realm_protector.bot import (
    composition,
    configuration_panel,
    configuration_remove,
    message_triggers,
    objectives,
    reaction_roles,
    tickets,
)
from src.realm_protector.bot.configuration_commands import (
    create_configuration_commands,
)
from src.realm_protector.bot.economy_commands import (
    create_economy_commands,
    create_prefix_economy_commands,
    register_economy_persistent_views,
)
from src.realm_protector.bot.feature_commands import create_feature_commands
from src.realm_protector.bot.registration_commands import (
    create_registration_commands,
)
from src.realm_protector.bot.sync_commands import create_sync_commands
from src.realm_protector.infrastructure import guild_settings
from src.realm_protector.services import (
    google_sync,
    local_maintenance,
    membership,
    registration,
    utc_timer,
)
from src.realm_protector.services.startup_notifications import load_startup_message

LOGGER = logging.getLogger(__name__)
_RUNTIME_RECONCILIATION_INTERVAL_SECONDS = 60


class RealmProtectorCommandTree(app_commands.CommandTree):
    """Route application-command failures through the owning bot."""

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        client = self.client
        if isinstance(client, RealmProtectorBot):
            await client._handle_application_command_error(interaction, error)
            return
        await super().on_error(interaction, error)


class RealmProtectorBot(commands.Bot):
    """Discord composition root for Realm Protector."""

    def __init__(self, project_root: Optional[Path] = None) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(
            command_prefix="!",
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
            tree_cls=RealmProtectorCommandTree,
        )

        self.project_root = (project_root or Path.cwd()).resolve()
        self._background_services_started = False
        self._startup_notifications_sent = False
        self._application_commands_registered = False
        self._application_commands_synced = False
        self._legacy_roles_migrated = False
        self._runtime_state_reconciled = False
        self._runtime_reconciliation_task: Optional[asyncio.Task] = None
        self._runtime_reconciliation_lock = asyncio.Lock()

        self.add_command(composition.create_composition_command(self))
        for command in create_prefix_economy_commands(self):
            self.add_command(command)
        self.add_listener(composition.on_message_in_thread, "on_message")
        self.add_listener(self._post_housri_gif, "on_message")

    async def _post_housri_gif(self, message: discord.Message) -> None:
        await message_triggers.post_housri_gif(
            message,
            self.project_root / "resources" / "gif" / "8x4qbf.gif",
        )

    async def setup_hook(self) -> None:
        if not self._application_commands_registered:
            command_groups: Iterable[list[app_commands.Command]] = (
                create_configuration_commands(self),
                create_registration_commands(self),
                create_economy_commands(self),
                create_feature_commands(self),
                create_sync_commands(self),
            )
            for command_group in command_groups:
                for command in command_group:
                    self.tree.add_command(command)
            self._application_commands_registered = True

        tickets.register_persistent_views(self)
        objectives.register_persistent_views(self)
        register_economy_persistent_views(self)
        await self._sync_application_commands()

    async def _sync_application_commands(self) -> bool:
        if self._application_commands_synced:
            return True
        try:
            await self.tree.sync()
        except Exception:
            LOGGER.exception(
                "Discord application-command sync failed; it will be retried after ready."
            )
            return False
        self._application_commands_synced = True
        return True

    async def on_ready(self) -> None:
        if not self._legacy_roles_migrated:
            for guild in self.guilds:
                try:
                    self._migrate_legacy_role_ids(guild)
                except Exception:
                    LOGGER.exception(
                        "Legacy role-ID migration failed for guild %s",
                        guild.id,
                    )
            self._legacy_roles_migrated = True

        if not self._application_commands_synced:
            await self._sync_application_commands()

        if not self._runtime_state_reconciled:
            await self._reconcile_runtime_state_once()
        self._start_runtime_reconciliation_loop()

        if not self._background_services_started:
            objectives.start_objectives_scheduler(self)
            membership.start_guild_member_tracker(self)
            google_sync.start_google_sync()
            local_maintenance.start_local_maintenance()
            utc_timer.start_utc_timer_scheduler(self)
            self._background_services_started = True
            try:
                await utc_timer.refresh_utc_timer_channels(self)
            except Exception:
                LOGGER.exception("Initial UTC timer refresh failed")

        if not self._startup_notifications_sent:
            self._startup_notifications_sent = True
            await self._send_startup_notifications()

        if self.user is not None:
            LOGGER.info("Logged in as %s (ID: %s)", self.user, self.user.id)

    async def _reconcile_runtime_state_once(self) -> bool:
        """Run every durable Discord reconciler without letting one starve another."""

        async with self._runtime_reconciliation_lock:
            reconciliation_failed = False
            reconcilers = (
                (
                    "configuration removal",
                    configuration_remove.reconcile_configuration_removals,
                ),
                (
                    "registration side effects",
                    registration.reconcile_registration_side_effects,
                ),
                ("configuration panel", configuration_panel.reconcile_configuration_panels),
                ("tickets", tickets.reconcile_tickets),
                ("compositions", composition.reconcile_compositions),
                (
                    "reaction-role panels",
                    reaction_roles.reconcile_reaction_role_panels,
                ),
                ("objective actions", objectives.reconcile_objective_actions),
            )
            for workflow_name, reconcile in reconcilers:
                try:
                    await reconcile(self)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    reconciliation_failed = True
                    LOGGER.exception(
                        "Discord runtime-state reconciliation failed for %s",
                        workflow_name,
                    )
            self._runtime_state_reconciled = not reconciliation_failed
            return self._runtime_state_reconciled

    def _start_runtime_reconciliation_loop(self) -> None:
        task = self._runtime_reconciliation_task
        if task is not None and not task.done():
            return
        self._runtime_reconciliation_task = asyncio.create_task(
            self._runtime_reconciliation_loop(),
            name="realm-protector-runtime-reconciliation",
        )

    async def _runtime_reconciliation_loop(self) -> None:
        """Retry durable work during the same process, not only after another restart."""

        await self.wait_until_ready()
        while not self.is_closed():
            await asyncio.sleep(_RUNTIME_RECONCILIATION_INTERVAL_SECONDS)
            try:
                await self._reconcile_runtime_state_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # The per-workflow runner already isolates normal failures. This
                # guard keeps the loop alive if bookkeeping itself ever fails.
                LOGGER.exception("Runtime-state reconciliation loop failed")

    async def _handle_application_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        LOGGER.error(
            "Unhandled application-command error for %s",
            getattr(getattr(interaction, "command", None), "qualified_name", "unknown"),
            exc_info=(type(error), error, error.__traceback__),
        )
        message = "That command could not be completed. Please try again."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            LOGGER.warning("Could not deliver application-command error response")

    async def on_command_error(
        self,
        context: commands.Context,
        error: commands.CommandError,
    ) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        LOGGER.error(
            "Unhandled prefix-command error for %s",
            getattr(getattr(context, "command", None), "qualified_name", "unknown"),
            exc_info=(type(error), error, error.__traceback__),
        )
        try:
            await context.send(
                "That command could not be completed. Please try again.",
                delete_after=10,
            )
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.warning("Could not deliver prefix-command error response")

    async def close(self) -> None:
        task = self._runtime_reconciliation_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        shutdown_results = await asyncio.gather(
            objectives.stop_objectives_scheduler(),
            membership.stop_guild_member_tracker(),
            google_sync.stop_google_sync(),
            local_maintenance.stop_local_maintenance(),
            utc_timer.stop_utc_timer_scheduler(),
            return_exceptions=True,
        )
        for worker_name, result in zip(
            (
                "objectives",
                "membership",
                "Google sync",
                "local maintenance",
                "UTC timer",
            ),
            shutdown_results,
            strict=True,
        ):
            if isinstance(result, BaseException):
                LOGGER.error(
                    "Background worker shutdown failed for %s",
                    worker_name,
                    exc_info=result,
                )
        self._background_services_started = False
        await super().close()

    @staticmethod
    def _migrate_legacy_role_ids(guild: discord.Guild) -> None:
        target_guild_name = guild_settings.get_target_guild(guild.id)
        if not target_guild_name:
            return

        def resolve_highest(role_names: list[str]) -> list[int]:
            resolved = []
            for role_name in role_names:
                matching_roles = [
                    role for role in guild.roles if role.name.casefold() == role_name.casefold()
                ]
                if matching_roles:
                    resolved.append(max(matching_roles, key=lambda role: role.position).id)
            return list(dict.fromkeys(resolved))

        caller_ids = guild_settings.get_caller_role_ids(guild.id)
        economy_ids = guild_settings.get_economy_manager_role_ids(guild.id)
        member_id = guild_settings.get_member_role_id(guild.id)
        if caller_ids and economy_ids and member_id:
            return

        resolved_caller_ids = caller_ids or resolve_highest(
            guild_settings.get_caller_roles(guild.id)
        )
        resolved_economy_ids = economy_ids or resolve_highest(
            guild_settings.get_economy_manager_roles(guild.id)
        )
        resolved_member_ids = (
            [member_id]
            if member_id
            else resolve_highest([guild_settings.get_member_role(guild.id)])
        )
        guild_settings.set_target_guild(
            guild.id,
            target_guild_name,
            guild_settings.get_member_role(guild.id),
            ", ".join(guild_settings.get_caller_roles(guild.id)),
            ", ".join(guild_settings.get_economy_manager_roles(guild.id)),
            guild_settings.get_leave_action(guild.id),
            member_role_id=resolved_member_ids[0] if resolved_member_ids else None,
            caller_role_ids=resolved_caller_ids,
            economy_manager_role_ids=resolved_economy_ids,
        )

    async def on_raw_reaction_add(
        self,
        payload: discord.RawReactionActionEvent,
    ) -> None:
        await reaction_roles.handle_raw_reaction_add(self, payload)

    async def on_raw_reaction_remove(
        self,
        payload: discord.RawReactionActionEvent,
    ) -> None:
        await reaction_roles.handle_raw_reaction_remove(self, payload)

    async def _send_startup_notifications(self) -> None:
        message = load_startup_message(
            self.project_root / "resources" / "messages" / "startup_notification.txt"
        )

        for guild_id, channel_id in guild_settings.get_all_bot_updates_channels().items():
            guild = self.get_guild(guild_id)
            if guild is None:
                continue

            channel = guild.get_channel(channel_id)
            if channel is None:
                try:
                    fetched_channel = await guild.fetch_channel(channel_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    continue
                if not isinstance(fetched_channel, discord.TextChannel):
                    continue
                channel = fetched_channel

            if not isinstance(channel, discord.TextChannel):
                continue

            bot_member = guild.me
            if bot_member is not None and not channel.permissions_for(bot_member).send_messages:
                continue

            try:
                await channel.send(message)
            except (discord.Forbidden, discord.HTTPException):
                LOGGER.warning(
                    "Failed to send startup notification in guild %s channel %s",
                    guild_id,
                    channel_id,
                )


__all__ = ["RealmProtectorBot"]
