"""Synchronous Albion Online API adapter with typed failure semantics.

The Discord application invokes this adapter through ``external_io.run_albion``.
Each executor worker owns a reusable ``requests.Session`` so connections are
pooled without sharing mutable Session state across threads.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, List, Optional
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.realm_protector import __version__
from src.realm_protector.domain.policies import select_exact_named_record

BASE_URL = "https://gameinfo-ams.albiononline.com/api/gameinfo"
SEARCH_ENDPOINT = "/search?q="
BATTLE_ENDPOINT = "/battles/"
PLAYER_ENDPOINT = "/players/"
REQUEST_TIMEOUT_SECONDS = 10
MAX_RETRY_AFTER_SECONDS = 10.0
_TRANSIENT_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
_THREAD_LOCAL = threading.local()


class AlbionAPIError(RuntimeError):
    """Base class for failures returned by the Albion integration."""


class AlbionNotFoundError(AlbionAPIError):
    """The requested stable Albion resource does not exist."""


class AlbionTransientError(AlbionAPIError):
    """A retryable network, rate-limit, or upstream service failure."""

    def __init__(self, message: str, *, retry_after_seconds: Optional[float] = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class AlbionResponseError(AlbionAPIError):
    """Albion returned a permanent HTTP error or malformed response."""


class _BoundedRetry(Retry):
    """Honor Retry-After without letting one worker sleep indefinitely."""

    def get_retry_after(self, response: Any) -> Optional[float]:
        retry_after = super().get_retry_after(response)
        if retry_after is None:
            return None
        return min(max(float(retry_after), 0.0), MAX_RETRY_AFTER_SECONDS)


def _retry_policy() -> Retry:
    return _BoundedRetry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.5,
        status_forcelist=tuple(sorted(_TRANSIENT_STATUS_CODES)),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )


def _new_session() -> requests.Session:
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=_retry_policy(), pool_connections=4, pool_maxsize=4)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": f"Realm-Protector-Discord-Bot/{__version__}"})
    return session


def _get_session() -> requests.Session:
    session = getattr(_THREAD_LOCAL, "session", None)
    if session is None:
        session = _new_session()
        _THREAD_LOCAL.session = session
    return session


def _retry_after_seconds(response: Any) -> Optional[float]:
    headers = getattr(response, "headers", {}) or {}
    raw_value = str(headers.get("Retry-After") or "").strip()
    if not raw_value:
        return None
    try:
        return max(float(raw_value), 0.0)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(raw_value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max((retry_at - datetime.now(timezone.utc)).total_seconds(), 0.0)


def _request_json(url: str) -> Any:
    try:
        response = _get_session().get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    except (requests.RequestException, OSError) as error:
        raise AlbionTransientError("Albion API is temporarily unavailable.") from error

    status_code = int(getattr(response, "status_code", 200) or 200)
    if status_code == 404:
        raise AlbionNotFoundError("The requested Albion resource was not found.")
    if status_code in _TRANSIENT_STATUS_CODES:
        raise AlbionTransientError(
            f"Albion API returned HTTP {status_code}.",
            retry_after_seconds=_retry_after_seconds(response),
        )
    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        raise AlbionResponseError(f"Albion API returned HTTP {status_code}.") from error

    try:
        return response.json()
    except (requests.JSONDecodeError, ValueError, TypeError) as error:
        raise AlbionResponseError("Albion API returned malformed JSON.") from error


def _get_search_url(query: str) -> str:
    return BASE_URL + SEARCH_ENDPOINT + quote(query, safe="")


def _search_players(nickname: str) -> list[dict]:
    query = str(nickname or "").strip()
    if not query:
        return []

    data = _request_json(_get_search_url(query))
    players = data.get("players", []) if isinstance(data, dict) else []
    if not isinstance(players, list):
        raise AlbionResponseError("Albion search response has an invalid players list.")
    return [player for player in players if isinstance(player, dict)]


def get_player_by_nickname(nickname: str) -> Optional[dict]:
    """Return only an exact Albion nickname match from search results."""

    exact = select_exact_named_record(_search_players(nickname), str(nickname or ""))
    return None if exact is None else dict(exact)


def search_players_by_nickname(nickname: str, *, limit: int = 3) -> list[dict]:
    """Return the first bounded player matches in Albion search order."""

    try:
        result_limit = int(limit)
    except (TypeError, ValueError):
        return []
    if result_limit <= 0:
        return []
    result_limit = min(result_limit, 3)

    matches: list[dict] = []
    seen_player_ids: set[str] = set()
    for player in _search_players(nickname):
        player_id = str(player.get("Id") or "").strip()
        player_name = str(player.get("Name") or "").strip()
        if not player_id or not player_name or player_id in seen_player_ids:
            continue
        seen_player_ids.add(player_id)
        matches.append(dict(player))
        if len(matches) >= result_limit:
            break
    return matches


def get_player_profile_by_id(player_id: str) -> Optional[dict]:
    player_key = str(player_id or "").strip()
    if not player_key:
        return None
    data = _request_json(BASE_URL + PLAYER_ENDPOINT + quote(player_key, safe=""))
    if not isinstance(data, dict):
        raise AlbionResponseError("Albion player profile response is invalid.")
    response_id = str(data.get("Id") or "").strip()
    if response_id and response_id != player_key:
        raise AlbionResponseError("Albion returned a profile for a different player ID.")
    return data


def get_battle_participants(battle_id: int) -> List[dict]:
    data = _request_json(BASE_URL + BATTLE_ENDPOINT + str(int(battle_id)))
    players = data.get("players", {}) if isinstance(data, dict) else {}
    if not isinstance(players, dict):
        raise AlbionResponseError("Albion battle response has an invalid players map.")
    return [
        participant
        for participant in players.values()
        if isinstance(participant, dict) and participant.get("name")
    ]


__all__ = [
    "AlbionAPIError",
    "AlbionNotFoundError",
    "AlbionResponseError",
    "AlbionTransientError",
    "get_battle_participants",
    "get_player_by_nickname",
    "get_player_profile_by_id",
    "search_players_by_nickname",
]
