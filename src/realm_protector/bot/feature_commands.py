from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands

from src.realm_protector.bot import objectives, reaction_roles, tickets
from src.realm_protector.services import utc_timer

if TYPE_CHECKING:
    from src.realm_protector.bot.client import RealmProtectorBot


def create_feature_commands(bot: "RealmProtectorBot") -> list[app_commands.Command]:
    @app_commands.command(
        name="tickets-setup",
        description="Configure ticket panels for guild applications",
    )
    @app_commands.guild_only()
    async def tickets_setup(interaction: discord.Interaction) -> None:
        await tickets.handle_tickets_setup(bot, interaction)

    @app_commands.command(
        name="role-reaction-setup",
        description="Set up role reaction panels for this server",
    )
    @app_commands.guild_only()
    async def role_reaction_setup(interaction: discord.Interaction) -> None:
        await reaction_roles.handle_role_reaction_setup(interaction)

    @app_commands.command(
        name="set-objective-panel",
        description="Post or update the objectives panel for this server",
    )
    @app_commands.guild_only()
    async def set_objective_panel(interaction: discord.Interaction) -> None:
        await objectives.handle_set_objectivess_panel(interaction)

    @app_commands.command(
        name="add-utc-timer",
        description="Append the current UTC time to the server name",
    )
    @app_commands.guild_only()
    async def add_utc_timer(interaction: discord.Interaction) -> None:
        await utc_timer.handle_add_utc_timer_slash(interaction)

    return [
        tickets_setup,
        role_reaction_setup,
        set_objective_panel,
        add_utc_timer,
    ]
