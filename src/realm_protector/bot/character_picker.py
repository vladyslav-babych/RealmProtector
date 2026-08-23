"""Reusable private Discord character-selection presentation."""

from __future__ import annotations

import discord

from src.realm_protector.services.albion_characters import (
    MAX_CHARACTER_OPTIONS,
    AlbionCharacterOption,
)


def _format_int(value: object) -> str:
    try:
        return f"{int(str(value)):,}"
    except (TypeError, ValueError):
        return "0"


def build_character_selection_embed(
    character_options: list[AlbionCharacterOption],
) -> discord.Embed:
    embed = discord.Embed(
        title="Select your character",
        description="Use button **1**, **2**, or **3** to select a character.",
    )
    for position, option in enumerate(character_options, start=1):
        search_profile = option.search_profile
        nickname = search_profile.get("Name") or "Unknown"
        guild_name = search_profile.get("GuildName") or "(no guild)"
        kill_fame = search_profile.get("KillFame") or 0
        death_fame = search_profile.get("DeathFame") or 0
        fame_ratio = search_profile.get("FameRatio")
        embed.add_field(
            name=f"{position}. {nickname}",
            value=(
                f"**Current guild:** {guild_name}\n"
                f"**Kill Fame:** {_format_int(kill_fame)}\n"
                f"**Death Fame:** {_format_int(death_fame)}\n"
                f"**Fame Ratio:** "
                f"{fame_ratio if fame_ratio is not None else '0'}\n"
                f"**PvE Fame:** {_format_int(option.pve_total)}"
            ),
            inline=False,
        )
    return embed


class _CharacterSelectionButton(discord.ui.Button):
    def __init__(self, position: int, *, disabled: bool = False):
        super().__init__(
            label=str(position),
            style=discord.ButtonStyle.primary,
            disabled=disabled,
        )
        self._option_index = position - 1

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, CharacterSelectionView):
            return
        await view.select(interaction, self._option_index)


class _CharacterSelectionCancelButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Cancel", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, CharacterSelectionView):
            return
        await view.cancel(interaction)


class CharacterSelectionView(discord.ui.View):
    """Private, invoker-bound picker specialized by feature subclasses."""

    def __init__(
        self,
        user_id: int,
        character_options: list[AlbionCharacterOption],
    ) -> None:
        super().__init__(timeout=300)
        self._selection_user_id = int(user_id)
        self._character_options = tuple(character_options[:MAX_CHARACTER_OPTIONS])
        for position in range(1, MAX_CHARACTER_OPTIONS + 1):
            self.add_item(
                _CharacterSelectionButton(
                    position,
                    disabled=position > len(self._character_options),
                )
            )
        self.add_item(_CharacterSelectionCancelButton())

    async def _ensure_owner(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self._selection_user_id:
            return True
        await interaction.response.send_message(
            "Only the user who searched for these characters can make this selection.",
            ephemeral=True,
        )
        return False

    async def select(
        self,
        interaction: discord.Interaction,
        option_index: int,
    ) -> None:
        if not await self._ensure_owner(interaction):
            return
        if option_index < 0 or option_index >= len(self._character_options):
            await interaction.response.send_message(
                "That character selection is unavailable.",
                ephemeral=True,
            )
            return
        await self.on_character_selected(
            interaction,
            self._character_options[option_index],
        )

    async def on_character_selected(
        self,
        interaction: discord.Interaction,
        selected_character: AlbionCharacterOption,
    ) -> None:
        raise NotImplementedError

    async def cancel(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_owner(interaction):
            return
        self.stop()
        await interaction.response.edit_message(
            content="Cancelled.",
            embed=None,
            view=None,
        )


__all__ = ["CharacterSelectionView", "build_character_selection_embed"]
