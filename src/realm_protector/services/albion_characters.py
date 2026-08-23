"""Albion character search options shared by interactive Discord workflows."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from src.realm_protector.infrastructure import albion_api, external_io

MAX_CHARACTER_OPTIONS = 3
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class AlbionCharacterOption:
    search_profile: dict
    pve_total: int
    player_profile: Optional[dict] = None

    @property
    def nickname(self) -> str:
        return str(self.search_profile.get("Name") or "").strip()

    @property
    def player_id(self) -> str:
        return str(self.search_profile.get("Id") or "").strip()


def extract_pve_fame(profile: dict) -> int:
    statistics = profile.get("LifetimeStatistics")
    if not isinstance(statistics, dict):
        return 0
    pve_statistics = statistics.get("PvE")
    if not isinstance(pve_statistics, dict):
        return 0
    try:
        return int(pve_statistics.get("Total") or 0)
    except (TypeError, ValueError):
        return 0


async def load_character_options(
    search_profiles: list[dict],
) -> list[AlbionCharacterOption]:
    async def load_option(search_profile: dict) -> AlbionCharacterOption:
        pve_total = 0
        verified_profile: Optional[dict] = None
        player_id = str(search_profile.get("Id") or "").strip()
        if player_id:
            try:
                player_profile = await external_io.run_albion(
                    albion_api.get_player_profile_by_id,
                    player_id,
                )
            except albion_api.AlbionAPIError as error:
                LOGGER.info(
                    "Could not enrich Albion character option %s: %s",
                    player_id,
                    error,
                )
                player_profile = None
            if isinstance(player_profile, dict):
                response_id = str(player_profile.get("Id") or "").strip()
                if response_id and response_id != player_id:
                    LOGGER.warning(
                        "Albion returned player ID %s for requested ID %s",
                        response_id,
                        player_id,
                    )
                else:
                    verified_profile = dict(player_profile)
                    verified_profile.setdefault("Id", player_id)
                    pve_total = extract_pve_fame(player_profile)
        return AlbionCharacterOption(
            dict(search_profile),
            pve_total,
            verified_profile,
        )

    limited_profiles = search_profiles[:MAX_CHARACTER_OPTIONS]
    return list(await asyncio.gather(*(load_option(profile) for profile in limited_profiles)))


async def search_character_options(
    nickname: str,
    *,
    raise_on_error: bool = False,
) -> list[AlbionCharacterOption]:
    try:
        search_profiles: object = await external_io.run_albion(
            albion_api.search_players_by_nickname,
            nickname,
        )
    except albion_api.AlbionAPIError:
        if raise_on_error:
            raise
        return []
    if not isinstance(search_profiles, list):
        return []
    return await load_character_options(search_profiles)


__all__ = [
    "MAX_CHARACTER_OPTIONS",
    "AlbionCharacterOption",
    "extract_pve_fame",
    "load_character_options",
    "search_character_options",
]
