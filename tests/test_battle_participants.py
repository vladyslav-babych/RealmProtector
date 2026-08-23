import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, call, patch

from src.realm_protector.services import battle_participants


class BattleParticipantCollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_deduplicates_battle_ids_before_fetching(self) -> None:
        def result_for_battle(battle_id: int):
            if battle_id == 12:
                return [
                    {"name": "Zulu", "guildName": "Realm"},
                    {"name": "alpha", "guildName": "realm"},
                    {"name": "Outsider", "guildName": "Other"},
                ]
            return [{"name": "Zulu", "guildName": "REALM"}]

        fetch_battle = Mock(side_effect=result_for_battle)

        result = await battle_participants.collect_battle_participants(
            "12, 12, 13,12",
            "Realm",
            fetch_battle,
        )

        self.assertEqual(("12", "13"), result.requested_ids)
        self.assertEqual(("alpha", "Zulu"), result.participant_names)
        self.assertEqual((), result.failed_ids)
        self.assertEqual([call(12), call(13)], fetch_battle.call_args_list)

    async def test_rejects_more_than_the_maximum_unique_battle_ids(self) -> None:
        fetch_battle = Mock(return_value=[])
        raw_ids = ",".join(str(value) for value in range(1, battle_participants.MAX_BATTLE_IDS + 2))

        with self.assertRaisesRegex(
            ValueError,
            f"at most {battle_participants.MAX_BATTLE_IDS} battle IDs",
        ):
            await battle_participants.collect_battle_participants(
                raw_ids,
                "Realm",
                fetch_battle,
            )

        fetch_battle.assert_not_called()

    async def test_non_positive_and_non_numeric_ids_fail_without_fetching(self) -> None:
        fetch_battle = Mock(return_value=[])

        result = await battle_participants.collect_battle_participants(
            "0, -5, invalid, 2, 0",
            "Realm",
            fetch_battle,
        )

        self.assertEqual(("0", "-5", "invalid", "2"), result.requested_ids)
        self.assertEqual(("0", "-5", "invalid"), result.failed_ids)
        self.assertEqual([call(2)], fetch_battle.call_args_list)

    async def test_fetches_are_concurrent_but_feature_bounded(self) -> None:
        active = 0
        maximum_active = 0

        async def run_albion(_fetcher, battle_id):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return [{"name": f"Player {battle_id}", "guildName": "Realm"}]

        with patch.object(
            battle_participants.external_io,
            "run_albion",
            new=AsyncMock(side_effect=run_albion),
        ):
            result = await battle_participants.collect_battle_participants(
                ",".join(str(value) for value in range(1, 11)),
                "Realm",
                Mock(),
            )

        self.assertEqual(
            battle_participants._BATTLE_FETCH_CONCURRENCY,
            maximum_active,
        )
        self.assertEqual(10, len(result.participant_names))


if __name__ == "__main__":
    unittest.main()
