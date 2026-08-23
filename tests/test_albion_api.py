from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import requests

from src.realm_protector.infrastructure import albion_api


class FakeResponse:
    def __init__(
        self,
        payload: object = None,
        *,
        status_code: int = 200,
        headers: dict | None = None,
        json_error: Exception | None = None,
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self._json_error = json_error

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> object:
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class AlbionExactMatchingTests(unittest.TestCase):
    def _session_for(self, response: FakeResponse) -> Mock:
        session = Mock()
        session.get.return_value = response
        return session

    def test_user_agent_identifies_the_application_release(self) -> None:
        from src.realm_protector import __version__

        session = albion_api._new_session()
        try:
            self.assertEqual(
                f"Realm-Protector-Discord-Bot/{__version__}",
                session.headers["User-Agent"],
            )
        finally:
            session.close()

    def test_selects_the_exact_case_insensitive_match_not_first_fuzzy_result(
        self,
    ) -> None:
        session = self._session_for(
            FakeResponse(
                {
                    "players": [
                        {"Id": "partial", "Name": "Exact Name Extra"},
                        {"Id": "wanted", "Name": "eXaCt NaMe"},
                    ]
                }
            )
        )

        with patch.object(albion_api, "_get_session", return_value=session):
            result = albion_api.get_player_by_nickname(" Exact Name ")

        self.assertEqual("wanted", result["Id"])
        requested_url = session.get.call_args.args[0]
        self.assertTrue(requested_url.endswith("Exact%20Name"), requested_url)
        self.assertEqual(
            albion_api.REQUEST_TIMEOUT_SECONDS,
            session.get.call_args.kwargs["timeout"],
        )

    def test_primary_player_lookup_rejects_a_fuzzy_first_result(self) -> None:
        session = self._session_for(
            FakeResponse(
                {
                    "players": [
                        {"Id": "partial", "Name": "TargetAlt"},
                        {"Id": "exact", "Name": "Target", "GuildName": "Realm"},
                    ]
                }
            )
        )

        with patch.object(albion_api, "_get_session", return_value=session):
            result = albion_api.get_player_by_nickname("target")

        self.assertEqual(
            {"Id": "exact", "Name": "Target", "GuildName": "Realm"},
            result,
        )

    def test_search_returns_first_three_unique_players_in_api_order(self) -> None:
        session = self._session_for(
            FakeResponse(
                {
                    "players": [
                        {"Name": "missing id"},
                        {"Id": "one", "Name": "Player"},
                        {"Id": "two", "Name": "PlayerOne"},
                        {"Id": "two", "Name": "Duplicate PlayerOne"},
                        {"Id": "three", "Name": "PlayerTwo"},
                        {"Id": "four", "Name": "PlayerThree"},
                    ]
                }
            )
        )

        with patch.object(albion_api, "_get_session", return_value=session):
            result = albion_api.search_players_by_nickname("Player", limit=3)

        self.assertEqual(["one", "two", "three"], [player["Id"] for player in result])

    def test_returns_none_when_search_has_no_exact_match(self) -> None:
        session = self._session_for(
            FakeResponse({"players": [{"Id": "partial", "Name": "Player Two"}]})
        )

        with patch.object(albion_api, "_get_session", return_value=session):
            result = albion_api.get_player_by_nickname("Player")

        self.assertIsNone(result)

    def test_ignores_malformed_search_entries(self) -> None:
        session = self._session_for(
            FakeResponse(
                {
                    "players": [
                        None,
                        "invalid",
                        {"Name": "Player"},
                        {"Id": 7, "Name": "Target"},
                    ]
                }
            )
        )

        with patch.object(albion_api, "_get_session", return_value=session):
            result = albion_api.get_player_by_nickname("target")

        self.assertEqual(7, result["Id"])

    def test_network_failure_is_typed_as_transient(self) -> None:
        session = Mock()
        session.get.side_effect = requests.ConnectionError("network unavailable")

        with (
            patch.object(albion_api, "_get_session", return_value=session),
            self.assertRaises(albion_api.AlbionTransientError),
        ):
            albion_api.get_player_by_nickname("Target")

    def test_profile_404_is_typed_as_not_found(self) -> None:
        session = self._session_for(FakeResponse(status_code=404))

        with (
            patch.object(albion_api, "_get_session", return_value=session),
            self.assertRaises(albion_api.AlbionNotFoundError),
        ):
            albion_api.get_player_profile_by_id("missing-id")

    def test_retry_after_is_exposed_on_transient_response(self) -> None:
        session = self._session_for(FakeResponse(status_code=429, headers={"Retry-After": "7"}))

        with patch.object(albion_api, "_get_session", return_value=session):
            with self.assertRaises(albion_api.AlbionTransientError) as captured:
                albion_api.get_player_profile_by_id("player-id")

        self.assertEqual(7.0, captured.exception.retry_after_seconds)

    def test_malformed_json_is_typed_as_response_error(self) -> None:
        session = self._session_for(FakeResponse(json_error=ValueError("invalid JSON")))

        with (
            patch.object(albion_api, "_get_session", return_value=session),
            self.assertRaises(albion_api.AlbionResponseError),
        ):
            albion_api.get_player_profile_by_id("player-id")

    def test_profile_id_mismatch_is_rejected(self) -> None:
        session = self._session_for(FakeResponse({"Id": "another-player"}))

        with (
            patch.object(albion_api, "_get_session", return_value=session),
            self.assertRaises(albion_api.AlbionResponseError),
        ):
            albion_api.get_player_profile_by_id("player-id")

    def test_retry_policy_retries_rate_limits_and_respects_retry_after(self) -> None:
        policy = albion_api._retry_policy()

        self.assertIn(429, policy.status_forcelist)
        self.assertTrue(policy.respect_retry_after_header)
        self.assertEqual(frozenset({"GET"}), policy.allowed_methods)
        self.assertEqual(
            albion_api.MAX_RETRY_AFTER_SECONDS,
            policy.get_retry_after(FakeResponse(headers={"Retry-After": "3600"})),
        )


if __name__ == "__main__":
    unittest.main()
