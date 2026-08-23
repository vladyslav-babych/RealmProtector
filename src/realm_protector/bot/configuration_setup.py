from typing import Optional, Tuple

import discord

from src.realm_protector.bot.configuration_panel import post_or_update_bot_configuration_message
from src.realm_protector.infrastructure import guild_settings, local_repository
from src.realm_protector.services import (
    configuration_lifecycle,
    guild_lifecycle,
    role_security,
)
from src.realm_protector.services.authorization import (
    is_admin,
)


def _format_role_mentions_by_ids(guild: discord.Guild, role_ids: list[int]) -> str:
    mentions: list[str] = []
    for role_id in role_ids:
        role = guild.get_role(int(role_id))
        if role is not None:
            mentions.append(role.mention)
    return ", ".join(mentions) if mentions else "Not selected"


def _resolve_role_names(guild: discord.Guild, role_ids: list[int]) -> list[str]:
    result: list[str] = []
    for role_id in role_ids:
        role = guild.get_role(int(role_id))
        if role is not None:
            result.append(role.name)
    return result


def _authorization_roles_error(
    guild: discord.Guild,
    role_ids: list[int],
) -> Optional[str]:
    if not role_ids:
        return "Select at least one role."
    for role_id in role_ids:
        error = role_security.privileged_assignment_error(
            guild.get_role(int(role_id)),
            guild,
        )
        if error:
            return error
    return None


def _format_channel_mention(channel_id: Optional[int]) -> str:
    if not channel_id:
        return "Not selected"
    return f"<#{channel_id}>"


def _format_leave_action(value: Optional[str]) -> str:
    normalized = (value or "").strip().lower()
    if normalized == "kick":
        return "Kick from server"
    if normalized == "remove_roles":
        return "Remove all roles"
    if normalized == "none":
        return "Do nothing"
    return "Not selected"


def _build_bot_setup_step_embed(view: "BotSetupStepView") -> discord.Embed:
    state = view.state
    embed = discord.Embed(title=f"Bot Setup - Step {view.step}/7")

    if view.step == 1:
        embed.description = "## :pencil: Set guild name via modal"
        embed.add_field(
            name="Selected guild name", value=state["target_guild_name"] or "Not set", inline=False
        )
    elif view.step == 2:
        embed.description = "## :ogre: Select Caller role(s) for comp creation commands"
        embed.add_field(
            name="Selected Caller role(s)",
            value=_format_role_mentions_by_ids(view.guild, state["caller_role_ids"]),
            inline=False,
        )
    elif view.step == 3:
        embed.description = (
            "## :moneybag: Select Economy Manager role(s) for lootsplit and balance commands"
        )
        embed.add_field(
            name="Selected Economy Manager role(s)",
            value=_format_role_mentions_by_ids(view.guild, state["economy_manager_role_ids"]),
            inline=False,
        )
    elif view.step == 4:
        embed.description = "## :monkey: Select Member role assigned after registration"
        embed.add_field(
            name="Selected Member role",
            value=_format_role_mentions_by_ids(view.guild, state["member_role_ids"]),
            inline=False,
        )
    elif view.step == 5:
        embed.description = "## :satellite: Select channel for bot updates"
        embed.add_field(
            name="Selected updates channel",
            value=_format_channel_mention(state["bot_updates_channel_id"]),
            inline=False,
        )
    elif view.step == 6:
        embed.description = "## :door: Choose action when player leaves the guild"
        embed.add_field(
            name="Selected action",
            value=_format_leave_action(state.get("leave_action")),
            inline=False,
        )
    else:
        embed.description = "## :clipboard: Review summary and confirm setup"
        embed.add_field(
            name="Guild name", value=state["target_guild_name"] or "Not set", inline=False
        )
        embed.add_field(
            name="Caller role(s)",
            value=_format_role_mentions_by_ids(view.guild, state["caller_role_ids"]),
            inline=False,
        )
        embed.add_field(
            name="Economy Manager role(s)",
            value=_format_role_mentions_by_ids(view.guild, state["economy_manager_role_ids"]),
            inline=False,
        )
        embed.add_field(
            name="Member role",
            value=_format_role_mentions_by_ids(view.guild, state["member_role_ids"]),
            inline=False,
        )
        embed.add_field(
            name="Bot updates channel",
            value=_format_channel_mention(state["bot_updates_channel_id"]),
            inline=False,
        )
        embed.add_field(
            name="Leave guild action",
            value=_format_leave_action(state.get("leave_action")),
            inline=False,
        )
    return embed


def _apply_server_setup(
    discord_server_id: int,
    target_guild_name: str,
    member_role_name: str = "Member",
    caller_role_name: str = "Caller",
    economy_manager_role_name: str = "Economy Manager",
    leave_action: str = "remove_roles",
    *,
    member_role_id: Optional[int] = None,
    caller_role_ids: Optional[list[int]] = None,
    economy_manager_role_ids: Optional[list[int]] = None,
    bot_updates_channel_id: Optional[int] = None,
) -> Tuple[bool, str]:
    target_guild_name = target_guild_name.strip()
    if not target_guild_name:
        return False, "Albion guild name cannot be empty."

    if configuration_lifecycle.get_pending_removal(discord_server_id) is not None:
        return (
            False,
            "The previous configuration is still being cleaned up. Please wait "
            "for the automatic retry before running setup again.",
        )

    existing_guild_name = guild_settings.get_target_guild(discord_server_id)
    if existing_guild_name:
        return False, f"This server is already set up with the **{existing_guild_name}** guild."

    existing_server_id = guild_settings.get_server_id_by_target_guild(target_guild_name)
    if existing_server_id and int(existing_server_id) != discord_server_id:
        return (
            False,
            f"The **{target_guild_name}** guild is already set up by another discord server. \n"
            f"If **{target_guild_name}** is your guild, please contact bot owner directly to resolve this conflict.",
        )

    try:
        guild_settings.set_target_guild(
            discord_server_id,
            target_guild_name,
            member_role_name,
            caller_role_name,
            economy_manager_role_name,
            leave_action,
            member_role_id=member_role_id,
            caller_role_ids=caller_role_ids,
            economy_manager_role_ids=economy_manager_role_ids,
            bot_updates_channel_id=bot_updates_channel_id,
        )
    except local_repository.TargetGuildConflictError:
        # The database constraint is authoritative.  This also closes the race
        # where two servers pass the friendly pre-check at the same time.
        return (
            False,
            f"The **{target_guild_name}** guild is already set up by another Discord server.",
        )
    return (
        True,
        f"Setup saved. Discord server ID **{discord_server_id}** is now mapped to the guild **{target_guild_name}**.",
    )


class BotSetupLeaveActionSelect(discord.ui.Select):
    def __init__(self, custom_id: str):
        options = [
            discord.SelectOption(label="Kick from server", value="kick"),
            discord.SelectOption(label="Remove all roles", value="remove_roles"),
            discord.SelectOption(label="Do nothing", value="none"),
        ]
        super().__init__(
            placeholder="Choose action when player leaves the guild",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=custom_id,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, BotSetupStepView):
            return
        if not await view.ensure_owner(interaction):
            return
        view.state["leave_action"] = self.values[0]
        await interaction.response.edit_message(embed=_build_bot_setup_step_embed(view), view=view)


class BotSetupGuildNameModal(discord.ui.Modal, title="Set Guild Name"):
    target_guild_name: discord.ui.TextInput["BotSetupGuildNameModal"] = discord.ui.TextInput(
        label="Enter your guild name",
        placeholder="Guild",
        required=True,
        max_length=30,
    )

    def __init__(self, parent_view: "BotSetupStepView"):
        super().__init__()
        self.parent_view = parent_view
        self.target_guild_name.default = parent_view.state["target_guild_name"]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.parent_view.state["target_guild_name"] = str(self.target_guild_name).strip()
        if self.parent_view.host_message is not None:
            await self.parent_view.host_message.edit(
                embed=_build_bot_setup_step_embed(self.parent_view),
                view=self.parent_view,
            )
        await interaction.response.send_message("Guild name updated.", ephemeral=True)


class BotSetupCallerRoleSelect(discord.ui.RoleSelect):
    def __init__(self, custom_id: str):
        super().__init__(
            placeholder="Select Caller role(s)", min_values=1, max_values=25, custom_id=custom_id
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, BotSetupStepView):
            return
        if not await view.ensure_owner(interaction):
            return
        for role in self.values:
            role_error = role_security.privileged_assignment_error(
                role,
                view.guild,
            )
            if role_error:
                await interaction.response.send_message(role_error, ephemeral=True)
                return
        view.state["caller_role_ids"] = [role.id for role in self.values]
        await interaction.response.edit_message(embed=_build_bot_setup_step_embed(view), view=view)


class BotSetupEconomyRoleSelect(discord.ui.RoleSelect):
    def __init__(self, custom_id: str):
        super().__init__(
            placeholder="Select Economy Manager role(s)",
            min_values=1,
            max_values=25,
            custom_id=custom_id,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, BotSetupStepView):
            return
        if not await view.ensure_owner(interaction):
            return
        for role in self.values:
            role_error = role_security.privileged_assignment_error(
                role,
                view.guild,
            )
            if role_error:
                await interaction.response.send_message(role_error, ephemeral=True)
                return
        view.state["economy_manager_role_ids"] = [role.id for role in self.values]
        await interaction.response.edit_message(embed=_build_bot_setup_step_embed(view), view=view)


class BotSetupMemberRoleSelect(discord.ui.RoleSelect):
    def __init__(self, custom_id: str):
        super().__init__(
            placeholder="Select Member role", min_values=1, max_values=1, custom_id=custom_id
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, BotSetupStepView):
            return
        if not await view.ensure_owner(interaction):
            return
        role = self.values[0]
        role_error = role_security.self_assignment_error(
            role,
            view.guild,
        )
        if role_error:
            await interaction.response.send_message(role_error, ephemeral=True)
            return
        view.state["member_role_ids"] = [role.id]
        await interaction.response.edit_message(embed=_build_bot_setup_step_embed(view), view=view)


class BotSetupUpdatesChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, custom_id: str):
        super().__init__(
            placeholder="Select updates channel",
            min_values=1,
            max_values=1,
            custom_id=custom_id,
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, BotSetupStepView):
            return
        if not await view.ensure_owner(interaction):
            return

        selected_channel = self.values[0]
        selected_channel_id = int(getattr(selected_channel, "id", 0))
        if selected_channel_id <= 0:
            await interaction.response.send_message(
                "Select a text channel for restart updates.", ephemeral=True
            )
            return

        view.state["bot_updates_channel_id"] = selected_channel_id
        await interaction.response.edit_message(embed=_build_bot_setup_step_embed(view), view=view)


class BotSetupStepView(discord.ui.View):
    def __init__(
        self, guild: discord.Guild, user_id: int, step: int = 1, state: Optional[dict] = None
    ):
        super().__init__(timeout=900)
        self.guild = guild
        self.user_id = user_id
        self.lifecycle_generation = guild_lifecycle.generation(guild.id)
        self.step = step
        self.state = state or {
            "target_guild_name": "",
            "caller_role_ids": [],
            "economy_manager_role_ids": [],
            "member_role_ids": [],
            "bot_updates_channel_id": None,
            "leave_action": "remove_roles",
        }
        self.host_message: Optional[discord.Message] = None
        self._render_nonce = 0
        self._build_items()

    async def ensure_owner(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Only the admin who started this setup can use these controls.", ephemeral=True
            )
            return False
        return True

    def _build_items(self) -> None:
        self._render_nonce += 1
        self.clear_items()
        self.add_item(BotSetupBackButton())
        if self.step == 1:
            self.add_item(BotSetupSetGuildNameButton())
            self.add_item(BotSetupContinueButton())
            self.add_item(BotSetupCancelButton())
        elif self.step == 2:
            self.add_item(
                BotSetupCallerRoleSelect(custom_id=f"bot-setup-caller-{self._render_nonce}")
            )
            self.add_item(BotSetupContinueButton())
        elif self.step == 3:
            self.add_item(
                BotSetupEconomyRoleSelect(custom_id=f"bot-setup-economy-{self._render_nonce}")
            )
            self.add_item(BotSetupContinueButton())
        elif self.step == 4:
            self.add_item(
                BotSetupMemberRoleSelect(custom_id=f"bot-setup-member-{self._render_nonce}")
            )
            self.add_item(BotSetupContinueButton())
        elif self.step == 5:
            self.add_item(
                BotSetupUpdatesChannelSelect(custom_id=f"bot-setup-updates-{self._render_nonce}")
            )
            self.add_item(BotSetupContinueButton())
        elif self.step == 6:
            self.add_item(
                BotSetupLeaveActionSelect(custom_id=f"bot-setup-leave-action-{self._render_nonce}")
            )
            self.add_item(BotSetupContinueButton())
        else:
            self.add_item(BotSetupFinishButton())
            self.add_item(BotSetupCancelButton())

    def next_step(self) -> None:
        self.step = min(7, self.step + 1)
        self._build_items()

    def previous_step(self) -> None:
        self.step = max(1, self.step - 1)
        self._build_items()


class BotSetupBackButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Back", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, BotSetupStepView):
            return
        if not await view.ensure_owner(interaction):
            return
        view.previous_step()
        await interaction.response.edit_message(embed=_build_bot_setup_step_embed(view), view=view)


class BotSetupSetGuildNameButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Set Guild Name", style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, BotSetupStepView):
            return
        if not await view.ensure_owner(interaction):
            return
        await interaction.response.send_modal(BotSetupGuildNameModal(view))


class BotSetupContinueButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Save and Continue", style=discord.ButtonStyle.success)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, BotSetupStepView):
            return
        if not await view.ensure_owner(interaction):
            return

        if view.step == 1 and not view.state["target_guild_name"].strip():
            await interaction.response.send_message("Set guild name first.", ephemeral=True)
            return
        if view.step == 2 and not view.state["caller_role_ids"]:
            await interaction.response.send_message(
                "Select at least one Caller role.", ephemeral=True
            )
            return
        if view.step == 3 and not view.state["economy_manager_role_ids"]:
            await interaction.response.send_message(
                "Select at least one Economy Manager role.", ephemeral=True
            )
            return
        if view.step == 4 and not view.state["member_role_ids"]:
            await interaction.response.send_message("Select Member role.", ephemeral=True)
            return
        if view.step == 5 and not view.state["bot_updates_channel_id"]:
            await interaction.response.send_message("Select updates channel.", ephemeral=True)
            return
        if view.step == 6 and not (view.state.get("leave_action") or "").strip():
            await interaction.response.send_message("Select leave guild action.", ephemeral=True)
            return

        view.next_step()
        await interaction.response.edit_message(embed=_build_bot_setup_step_embed(view), view=view)


class BotSetupFinishButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Confirm Setup", style=discord.ButtonStyle.success)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, BotSetupStepView):
            return
        if not await view.ensure_owner(interaction):
            return
        if not isinstance(interaction.user, discord.Member) or not await is_admin(interaction.user):
            await interaction.response.send_message(
                "Your Administrator permission was removed; setup was not saved.",
                ephemeral=True,
            )
            return

        if not view.state["member_role_ids"]:
            await interaction.response.send_message("Select Member role.", ephemeral=True)
            return
        if not view.state["bot_updates_channel_id"]:
            await interaction.response.send_message("Select updates channel.", ephemeral=True)
            return
        if not (view.state.get("leave_action") or "").strip():
            await interaction.response.send_message("Select leave guild action.", ephemeral=True)
            return

        caller_role_error = _authorization_roles_error(
            view.guild,
            view.state["caller_role_ids"],
        )
        if caller_role_error:
            await interaction.response.send_message(
                f"Caller role configuration error: {caller_role_error}",
                ephemeral=True,
            )
            return
        economy_role_error = _authorization_roles_error(
            view.guild,
            view.state["economy_manager_role_ids"],
        )
        if economy_role_error:
            await interaction.response.send_message(
                f"Economy Manager role configuration error: {economy_role_error}",
                ephemeral=True,
            )
            return
        member_role = view.guild.get_role(int(view.state["member_role_ids"][0]))
        member_role_error = role_security.self_assignment_error(
            member_role,
            view.guild,
        )
        if member_role_error:
            await interaction.response.send_message(
                f"Member role configuration error: {member_role_error}",
                ephemeral=True,
            )
            return

        caller_role_names = _resolve_role_names(view.guild, view.state["caller_role_ids"])
        economy_manager_role_names = _resolve_role_names(
            view.guild, view.state["economy_manager_role_ids"]
        )
        member_role_names = _resolve_role_names(view.guild, view.state["member_role_ids"])

        async with guild_lifecycle.lock_for(view.guild.id):
            if not guild_lifecycle.is_current(
                view.guild.id,
                view.lifecycle_generation,
            ):
                success = False
                message = (
                    "The server configuration changed while this wizard was open. "
                    "Restart **/bot-setup**."
                )
            elif not isinstance(interaction.user, discord.Member) or not await is_admin(
                interaction.user
            ):
                success = False
                message = "Your Administrator permission was removed; setup was not saved."
            else:
                success, message = _apply_server_setup(
                    view.guild.id,
                    view.state["target_guild_name"].strip(),
                    member_role_names[0] if member_role_names else "Member",
                    ", ".join(caller_role_names) if caller_role_names else "Caller",
                    ", ".join(economy_manager_role_names)
                    if economy_manager_role_names
                    else "Economy Manager",
                    view.state.get("leave_action") or "remove_roles",
                    member_role_id=int(view.state["member_role_ids"][0]),
                    caller_role_ids=[int(role_id) for role_id in view.state["caller_role_ids"]],
                    economy_manager_role_ids=[
                        int(role_id) for role_id in view.state["economy_manager_role_ids"]
                    ],
                    bot_updates_channel_id=int(view.state["bot_updates_channel_id"]),
                )
                if success:
                    view.lifecycle_generation = guild_lifecycle.advance(view.guild.id)
        if not success:
            await interaction.response.send_message(message, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        if interaction.message is not None:
            await interaction.message.edit(
                embed=discord.Embed(title="Bot Setup", description="Setup completed."),
                view=None,
            )

        posted, posted_message = await post_or_update_bot_configuration_message(interaction)
        if not posted:
            await interaction.followup.send(posted_message, ephemeral=True)
            return
        await interaction.followup.send(posted_message, ephemeral=True)


class BotSetupCancelButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Cancel", style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, BotSetupStepView):
            return
        if not await view.ensure_owner(interaction):
            return
        await interaction.response.edit_message(
            embed=discord.Embed(title="Bot Setup", description="Setup cancelled."),
            view=None,
        )
