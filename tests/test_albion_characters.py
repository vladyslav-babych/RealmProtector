import unittest
from unittest.mock import AsyncMock, patch

from src.realm_protector.infrastructure import albion_api
from src.realm_protector.services import albion_characters


class AlbionCharacterOptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_options_retain_the_verified_stable_id_profile(self) -> None:
        search_profiles = [
            {"Id": "player-1", "Name": "Player One"},
            {"Id": "player-2", "Name": "Player Two"},
        ]

        async def run_albion(_function, player_id):
            return {
                "Id": player_id,
                "Name": f"Verified {player_id}",
                "LifetimeStatistics": {"PvE": {"Total": 1234}},
            }

        with patch.object(
            albion_characters.external_io,
            "run_albion",
            new=AsyncMock(side_effect=run_albion),
        ):
            options = await albion_characters.load_character_options(search_profiles)

        self.assertEqual(["player-1", "player-2"], [item.player_id for item in options])
        self.assertEqual([1234, 1234], [item.pve_total for item in options])
        self.assertEqual("player-1", options[0].player_profile["Id"])

    async def test_mismatched_profile_id_is_never_attached_to_selection(self) -> None:
        with patch.object(
            albion_characters.external_io,
            "run_albion",
            new=AsyncMock(return_value={"Id": "different-player"}),
        ):
            options = await albion_characters.load_character_options(
                [{"Id": "selected-player", "Name": "Player"}]
            )

        self.assertIsNone(options[0].player_profile)
        self.assertEqual(0, options[0].pve_total)

    async def test_search_can_distinguish_api_outage_from_no_matches(self) -> None:
        failure = albion_api.AlbionTransientError("temporarily unavailable")
        with patch.object(
            albion_characters.external_io,
            "run_albion",
            new=AsyncMock(side_effect=failure),
        ):
            self.assertEqual(
                [],
                await albion_characters.search_character_options("Player"),
            )
            with self.assertRaises(albion_api.AlbionTransientError):
                await albion_characters.search_character_options(
                    "Player",
                    raise_on_error=True,
                )


if __name__ == "__main__":
    unittest.main()
