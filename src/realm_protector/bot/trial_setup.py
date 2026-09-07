"""Admin-only trial setup; drafts and component routing survive process restarts."""

from __future__ import annotations

import logging
from uuid import uuid4

import discord

from src.realm_protector.bot import trials
from src.realm_protector.infrastructure import runtime_state, sqlite_database, trial_store
from src.realm_protector.services import authorization, guild_lifecycle

LOGGER = logging.getLogger(__name__)
STEPS = (
    ("category_id", "Trial category"),
    ("role_id", "Trial role"),
    ("manager_role_ids", "Trial Manager roles"),
    ("archive_channel_id", "Trial archive channel"),
    ("title", "Trial panel title"),
    ("message", "Trial panel message"),
    ("confirm", "Overview and confirmation"),
)


def draft_for(interaction):
    if interaction.guild is None or not authorization.member_is_admin(interaction.user):
        raise ValueError("Only server administrators can configure trials.")
    draft = runtime_state.get_record(trial_store.DRAFT, interaction.guild.id, interaction.user.id)
    if draft is None or draft.status != "active":
        raise ValueError("This setup has ended. Run /trial-setup to start again.")
    if (
        interaction.message is None
        or draft.payload.get("setup_message_id") != interaction.message.id
    ):
        raise ValueError("This setup menu is outdated. Use your latest /trial-setup menu.")
    return draft


def _selection_preview(payload: dict, field: str, guild=None) -> str:
    value = payload.get(field)
    if not value:
        return "Not set" if field in {"title", "message"} else "Not selected"
    if field in {"category_id", "archive_channel_id"}:
        if guild is not None:
            channel = guild.get_channel(int(value))
            if channel is None:
                return "Not selected"
            return channel.name if field == "category_id" else channel.mention
        return f"<#{value}>"
    if field in {"role_id", "manager_role_ids"}:
        role_ids = [value] if field == "role_id" else value
        mentions = [
            f"<@&{role_id}>"
            for role_id in role_ids
            if guild is None or guild.get_role(int(role_id)) is not None
        ]
        return ", ".join(mentions) or "Not selected"
    return str(value)


def _add_preview_field(embed: discord.Embed, name: str, value: str) -> None:
    """Keep long configured messages readable without exceeding Discord field limits."""
    for offset in range(0, len(value), 1024):
        embed.add_field(
            name=name if offset == 0 else f"{name} (continued)",
            value=value[offset : offset + 1024],
            inline=False,
        )


def setup_embed(payload: dict, guild=None) -> discord.Embed:
    step = payload.get("step", 0)
    embed = discord.Embed(title=f"Trial Setup - Step {step + 1}/7")
    instructions = (
        ("## :open_file_folder: Select the trial category", "Selected category"),
        ("## :shield: Select the Trial role assigned to the player", "Selected Trial role"),
        ("## :tickets: Select the Trial Manager role(s)", "Selected Trial Manager roles"),
        ("## :file_folder: Select the trial archive channel", "Selected archive channel"),
        ("## :pencil: Set the trial panel title", "Panel title"),
        ("## :speech_balloon: Set the trial panel message", "Panel message"),
    )
    if step < 6:
        embed.description, field_label = instructions[step]
        _add_preview_field(embed, field_label, _selection_preview(payload, STEPS[step][0], guild))
    else:
        embed.description = "## :clipboard: Review the summary and confirm trial setup"
        for field, label in STEPS[:5]:
            _add_preview_field(embed, label, _selection_preview(payload, field, guild))
        _add_preview_field(
            embed,
            "Panel preview",
            f"**{_selection_preview(payload, 'title', guild)}**\n{_selection_preview(payload, 'message', guild)}",
        )
    return embed


class TrialChannelSelect(discord.ui.ChannelSelect):
    async def callback(self, interaction):
        if isinstance(self.view, SetupView):
            await self.view.select_value(interaction)


class TrialRoleSelect(discord.ui.RoleSelect):
    async def callback(self, interaction):
        if isinstance(self.view, SetupView):
            await self.view.select_value(interaction)


class SetupView(discord.ui.View):
    def __init__(self, step=0, payload=None, guild=None):
        super().__init__(timeout=None)
        payload = payload or {}
        # Match the other setup wizards: navigation above the selector, with
        # only the current step's controls present. Preserve stable routing IDs.
        self.clear_items()
        self.add_item(self.back)
        if step in (0, 3):
            selected_id = payload.get(STEPS[step][0])
            if selected_id and guild is not None and guild.get_channel(int(selected_id)) is None:
                selected_id = None
            select = TrialChannelSelect(
                placeholder="Select the trial category"
                if step == 0
                else "Select the trial archive channel",
                channel_types=[
                    discord.ChannelType.category if step == 0 else discord.ChannelType.text
                ],
                custom_id=f"realm:trial:setup:select:{step}",
                default_values=[discord.Object(id=selected_id)] if selected_id else [],
                row=1,
            )
            self.add_item(select)
        elif step in (1, 2):
            role_ids = (
                ([payload["role_id"]] if payload.get("role_id") else [])
                if step == 1
                else payload.get("manager_role_ids", [])
            )
            if guild is not None:
                role_ids = [
                    role_id for role_id in role_ids if guild.get_role(int(role_id)) is not None
                ]
            roles = TrialRoleSelect(
                placeholder="Select Trial role" if step == 1 else "Select Trial Manager role(s)",
                min_values=1,
                max_values=1 if step == 1 else 25,
                custom_id=f"realm:trial:setup:select:{step}",
                default_values=[discord.Object(id=role_id) for role_id in role_ids],
                row=1,
            )
            self.add_item(roles)
        elif step in (4, 5):
            self.edit.label = "Set Panel Title" if step == 4 else "Set Panel Message"
            self.add_item(self.edit)
        if step == 6:
            self.add_item(self.confirm)
        else:
            self.add_item(self.next)
        if step in (0, 6):
            self.add_item(self.cancel)

    async def interaction_check(self, interaction):
        try:
            draft_for(interaction)
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return False
        return True

    async def select_value(self, interaction):
        await interaction.response.defer()
        async with guild_lifecycle.lock_for(interaction.guild.id):
            draft = draft_for(interaction)
            step = draft.payload["step"]
            if interaction.data["custom_id"] != f"realm:trial:setup:select:{step}":
                await interaction.followup.send(
                    "The setup step changed. Use the current menu.", ephemeral=True
                )
                return
            values = [int(value) for value in interaction.data["values"]]
            field = STEPS[step][0]
            draft = trial_store.save(draft, **{field: values if step == 2 else values[0]})
            await interaction.edit_original_response(
                embed=setup_embed(draft.payload, interaction.guild),
                view=SetupView(step, draft.payload, interaction.guild),
            )

    async def move(self, interaction, delta):
        await interaction.response.defer()
        async with guild_lifecycle.lock_for(interaction.guild.id):
            draft = draft_for(interaction)
            step = draft.payload["step"]
            if delta > 0 and not draft.payload.get(STEPS[step][0]):
                await interaction.followup.send(
                    f"{'Set' if step in (4, 5) else 'Select'} {STEPS[step][1].lower()} first.",
                    ephemeral=True,
                )
                return
            step = max(0, min(6, step + delta))
            draft = trial_store.save(draft, step=step)
            await interaction.edit_original_response(
                embed=setup_embed(draft.payload, interaction.guild),
                view=SetupView(step, draft.payload, interaction.guild),
            )

    @discord.ui.button(
        label="Back", style=discord.ButtonStyle.secondary, custom_id="realm:trial:setup:back", row=0
    )
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.move(interaction, -1)

    @discord.ui.button(
        label="Save and Continue",
        style=discord.ButtonStyle.success,
        custom_id="realm:trial:setup:next",
        row=0,
    )
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.move(interaction, 1)

    @discord.ui.button(
        label="Set Panel Title",
        style=discord.ButtonStyle.primary,
        custom_id="realm:trial:setup:edit",
        row=0,
    )
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        draft = draft_for(interaction)
        if draft.payload["step"] not in (4, 5):
            await interaction.response.send_message("Use the current setup step.", ephemeral=True)
            return
        await interaction.response.send_modal(PanelTextModal(draft))

    @discord.ui.button(
        label="Confirm Setup",
        style=discord.ButtonStyle.success,
        custom_id="realm:trial:setup:confirm",
        row=0,
    )
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            return
        await interaction.response.defer()
        async with guild_lifecycle.lock_for(interaction.guild.id):
            draft = draft_for(interaction)
            if draft.payload["step"] != 6:
                await interaction.followup.send("Complete all setup steps first.", ephemeral=True)
                return
            try:
                trials.validate_configuration(interaction.guild, draft.payload)
                channel = interaction.channel
                if not isinstance(channel, discord.TextChannel):
                    raise ValueError("Run /trial-setup in a server text channel.")
                permissions = channel.permissions_for(interaction.guild.me)
                if not all(
                    getattr(permissions, name)
                    for name in (
                        "view_channel",
                        "send_messages",
                        "read_message_history",
                        "embed_links",
                    )
                ):
                    raise ValueError(
                        "The bot needs View Channel, Send Messages, Read History and Embed Links here to publish the configuration panel."
                    )
            except ValueError as error:
                await interaction.followup.send(str(error), ephemeral=True)
                return
            payload = {key: draft.payload[key] for key, _ in STEPS[:-1]}
            old = trial_store.config(interaction.guild.id)
            previous_channel = None
            if old is not None:
                try:
                    previous_channel = await interaction.guild.fetch_channel(
                        old.payload["panel_channel_id"]
                    )
                except discord.NotFound:
                    pass
            if old is not None and previous_channel is not None and old.status == "publishing":
                await interaction.followup.send(
                    "The previous configuration panel is still being published. Please retry shortly.",
                    ephemeral=True,
                )
                return
            # Preserve one panel; a deleted destination can be replaced by rerunning setup.
            payload.update(
                panel_channel_id=previous_channel.id if previous_channel else channel.id,
                publication_id=uuid4().hex,
            )
            if old is not None and previous_channel is not None:
                payload["message_id"] = old.payload.get("message_id")
            with sqlite_database.transaction() as database:
                record = runtime_state.upsert_record_in_transaction(
                    database,
                    trial_store.CONFIG,
                    interaction.guild.id,
                    "main",
                    payload,
                    status="publishing",
                )
                runtime_state.upsert_record_in_transaction(
                    database,
                    draft.kind,
                    draft.guild_id,
                    draft.external_id,
                    draft.payload,
                    status="completed",
                )
            try:
                await trials.publish_configuration(interaction.guild, record)
                text = "Trial configuration saved and panel posted. Use /trial-add @User to begin a trial."
            except Exception:
                LOGGER.exception("Trial configuration panel publication failed")
                text = "Trial configuration saved. Panel publication is pending and will retry automatically."
            await interaction.edit_original_response(content=text, embed=None, view=None)

    @discord.ui.button(
        label="Cancel Setup",
        style=discord.ButtonStyle.danger,
        custom_id="realm:trial:setup:cancel",
        row=0,
    )
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            return
        await interaction.response.defer()
        async with guild_lifecycle.lock_for(interaction.guild.id):
            draft = draft_for(interaction)
            trial_store.save(draft, status="cancelled")
            await interaction.edit_original_response(
                content="Trial setup cancelled. Existing configuration is unchanged.",
                embed=None,
                view=None,
            )


class PanelTextModal(discord.ui.Modal):
    def __init__(self, draft):
        self.step = draft.payload["step"]
        self.session_id = draft.payload["session_id"]
        self.field, label = STEPS[self.step]
        super().__init__(title=f"Set {label.title()}", timeout=600)
        self.value: discord.ui.TextInput = discord.ui.TextInput(
            label=label,
            placeholder="Welcome to your trial"
            if self.step == 4
            else "Enter the message shown when a player's trial starts.",
            default=draft.payload.get(self.field, ""),
            style=discord.TextStyle.paragraph if self.step == 5 else discord.TextStyle.short,
            max_length=4000 if self.step == 5 else 256,
            required=True,
        )
        self.add_item(self.value)

    async def on_submit(self, interaction):
        await interaction.response.defer()
        async with guild_lifecycle.lock_for(interaction.guild.id):
            try:
                draft = draft_for(interaction)
                if (
                    draft.payload["session_id"] != self.session_id
                    or draft.payload["step"] != self.step
                ):
                    raise ValueError(
                        "This text entry is outdated. Reopen the current step's text editor."
                    )
                if not self.value.value.strip():
                    raise ValueError("Panel text cannot be empty.")
            except ValueError as error:
                await interaction.followup.send(str(error), ephemeral=True)
                return
            draft = trial_store.save(draft, **{self.field: self.value.value.strip()})
            await interaction.edit_original_response(
                embed=setup_embed(draft.payload, interaction.guild),
                view=SetupView(self.step, draft.payload, interaction.guild),
            )
            await interaction.followup.send(f"Panel {self.field} updated.", ephemeral=True)


async def handle_trial_setup(interaction: discord.Interaction):
    if interaction.guild is None or not authorization.member_is_admin(interaction.user):
        await interaction.response.send_message(
            "Only server administrators can configure trials.", ephemeral=True
        )
        return
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message(
            "Run /trial-setup in a server text channel.", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True)
    async with guild_lifecycle.lock_for(interaction.guild.id):
        draft = runtime_state.get_record(
            trial_store.DRAFT, interaction.guild.id, interaction.user.id
        )
        configuration = trial_store.config(interaction.guild.id)
        payload = (
            dict(draft.payload)
            if draft is not None and draft.status == "active"
            else {
                **(configuration.payload if configuration else {}),
                "step": 0,
            }
        )
        payload["session_id"] = uuid4().hex
        draft = runtime_state.upsert_record(
            trial_store.DRAFT, interaction.guild.id, interaction.user.id, payload
        )
        message = await interaction.edit_original_response(
            embed=setup_embed(payload, interaction.guild),
            view=SetupView(payload["step"], payload, interaction.guild),
        )
        trial_store.save(draft, setup_message_id=message.id)
