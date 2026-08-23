from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Optional

from src.realm_protector.domain.models import BattleParticipantsResult
from src.realm_protector.domain.policies import is_in_target_guild
from src.realm_protector.infrastructure import albion_api, external_io

BattleFetcher = Callable[[int], Optional[list[dict]]]
MAX_BATTLE_IDS = 10
_BATTLE_FETCH_CONCURRENCY = 4


async def collect_battle_participants(
    raw_battle_ids: str,
    target_guild_name: str,
    fetch_battle: BattleFetcher,
) -> BattleParticipantsResult:
    """Fetch battles and return a deterministic target-guild participant set."""

    requested_ids = tuple(
        dict.fromkeys(
            battle_id.strip()
            for battle_id in (raw_battle_ids or "").split(",")
            if battle_id.strip()
        )
    )
    if len(requested_ids) > MAX_BATTLE_IDS:
        raise ValueError(f"Provide at most {MAX_BATTLE_IDS} battle IDs at a time.")
    parsed_ids: dict[str, int] = {}
    for raw_battle_id in requested_ids:
        try:
            battle_id = int(raw_battle_id)
        except ValueError:
            continue
        if battle_id <= 0:
            continue
        parsed_ids[raw_battle_id] = battle_id

    semaphore = asyncio.Semaphore(_BATTLE_FETCH_CONCURRENCY)

    async def fetch_one(battle_id: int) -> Optional[list[dict]]:
        async with semaphore:
            try:
                return await external_io.run_albion(fetch_battle, battle_id)
            except albion_api.AlbionAPIError:
                return None
            except Exception:
                return None

    fetched = await asyncio.gather(*(fetch_one(battle_id) for battle_id in parsed_ids.values()))
    fetched_by_raw_id = {
        raw_battle_id: fetched[index] for index, raw_battle_id in enumerate(parsed_ids)
    }

    participants: set[str] = set()
    failed_ids: list[str] = []
    for raw_battle_id in requested_ids:
        parsed_battle_id = parsed_ids.get(raw_battle_id)
        if parsed_battle_id is None:
            failed_ids.append(raw_battle_id)
            continue
        battle_participants = fetched_by_raw_id[raw_battle_id]
        if not isinstance(battle_participants, list):
            failed_ids.append(str(parsed_battle_id))
            continue
        participant_records = (
            participant for participant in battle_participants if isinstance(participant, dict)
        )
        for participant in participant_records:
            name = str(participant.get("name") or "").strip()
            if name and is_in_target_guild(
                participant.get("guildName"),
                target_guild_name,
            ):
                participants.add(name)

    return BattleParticipantsResult(
        requested_ids=requested_ids,
        participant_names=tuple(sorted(participants, key=lambda name: (name.casefold(), name))),
        failed_ids=tuple(failed_ids),
    )
