"""Local-first Google Sheet projection and Sheet-owned Siphon synchronization.

SQLite owns registrations, membership state, Silver, and audit history.  Google
Sheets is an optional projection of those fields and the calculation engine for
Siphon.  The only routine Google-to-local flow after bootstrap is Siphon.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import unicodedata
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence
from uuid import uuid4
from weakref import WeakValueDictionary

from src.realm_protector.infrastructure import (
    credential_store,
    document_store,
    external_io,
    google_sheets,
    guild_settings,
    local_repository,
)

LOGGER = logging.getLogger(__name__)
SYNC_INTERVAL_SECONDS = 300
SIPHON_STALE_AFTER_SECONDS = 15 * 60
MAX_OUTBOX_ATTEMPTS = 8
_PLAYERS_PROJECTION_HEADERS = [
    *google_sheets.PLAYERS_REQUIRED_HEADERS,
    "Siphon",
    "Realm Registration ID",
    "Realm Revision",
]
_BALANCE_EVENT_HEADER = "Realm Event ID"
_LOOTSPLIT_EVENT_HEADER = "Realm Event ID"
_sync_task: Optional[asyncio.Task] = None
_SYNC_LOCKS_GUARD = threading.Lock()
_SYNC_LOCKS: WeakValueDictionary[int, Any] = WeakValueDictionary()


def _sync_lock(guild_id: int) -> threading.RLock:
    """Serialize every Google workflow for one guild, including slash refreshes."""

    with _SYNC_LOCKS_GUARD:
        return _SYNC_LOCKS.setdefault(int(guild_id), threading.RLock())


@dataclass(frozen=True)
class SyncResult:
    success: bool
    message: str
    processed_events: int = 0
    updated_siphon_rows: int = 0
    imported_rows: int = 0
    expected_siphon_rows: int = 0
    rejected_siphon_rows: int = 0
    incomplete: bool = False


def _active_ledger(
    discord_guild_id: int,
    credentials_info: Optional[Mapping[str, Any]] = None,
) -> local_repository.LedgerGeneration:
    if credentials_info is not None:
        link_status = str(credentials_info.get("status") or "active").strip().casefold()
        if (
            link_status != "active"
            or credentials_info.get("disabled") is True
            or credentials_info.get("quarantined") is True
        ):
            raise ValueError(
                "The Google Sheet link is disabled or quarantined. Relink it before synchronization."
            )
    configured_target = guild_settings.get_target_guild(discord_guild_id)
    linked_target = str((credentials_info or {}).get("guild_name") or "").strip()
    configured_key = (
        unicodedata.normalize(
            "NFKC",
            str(configured_target or ""),
        )
        .strip()
        .casefold()
    )
    linked_key = unicodedata.normalize("NFKC", linked_target).strip().casefold()
    if not configured_key:
        raise RuntimeError("This Discord server is not configured for an Albion guild.")
    if not linked_key:
        raise ValueError(
            "The Google Sheet link has no Albion guild identity. Relink it before synchronization."
        )
    if configured_key != linked_key:
        raise ValueError(
            "The linked Google Sheet belongs to a different configured Albion guild. "
            "Relink Google Sheets for the current guild before cutover."
        )
    ledger = local_repository.get_active_ledger(
        discord_guild_id,
        create_if_missing=False,
    )
    if ledger is None:
        raise RuntimeError("No active local ledger is configured for this server.")
    if not ledger.target_guild_key or ledger.target_guild_key != configured_key:
        raise RuntimeError("The active ledger does not match the configured Albion guild.")
    return ledger


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _row_fingerprint(row: Sequence[Any]) -> str:
    return hashlib.sha256(_canonical_json(list(row)).encode("utf-8")).hexdigest()


def _parse_integer(value: Any, *, allow_blank: bool = False) -> Optional[int]:
    if isinstance(value, bool):
        raise ValueError("boolean is not an integer cell value")
    if value is None:
        if allow_blank:
            return None
        return 0
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise ValueError("integer cell contains a fractional number")
        parsed = int(value)
    else:
        normalized = str(value).replace(" ", "").replace(",", "").strip()
        if not normalized and allow_blank:
            return None
        if not normalized:
            return 0
        parsed = int(normalized)
    if parsed < local_repository.SQLITE_INTEGER_MIN or parsed > local_repository.SQLITE_INTEGER_MAX:
        raise ValueError("integer is outside SQLite's signed 64-bit range")
    return parsed


def _cell(row: Sequence[Any], index: int) -> str:
    return str(row[index] if len(row) > index else "").strip()


def _configured_sheet_identity(
    guild_id: int,
    credentials_info: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the stable logical Sheet mapping, excluding replaceable credentials."""

    return {
        "version": 2,
        "guild_id": guild_id,
        "guild_name": credentials_info.get("guild_name"),
        "google_sheet_name": credentials_info.get("google_sheet_name"),
        "google_worksheet_name": credentials_info.get("google_worksheet_name"),
        "lootsplit_history_worksheet_name": credentials_info.get(
            "lootsplit_history_worksheet_name"
        ),
        "balance_history_worksheet_name": credentials_info.get("balance_history_worksheet_name"),
    }


def _remote_worksheet_identity(worksheet: Any) -> dict[str, Any]:
    spreadsheet_id = getattr(worksheet, "spreadsheet_id", None)
    if spreadsheet_id is None:
        spreadsheet_id = getattr(getattr(worksheet, "spreadsheet", None), "id", None)
    return {
        "spreadsheet_id": spreadsheet_id,
        "worksheet_id": getattr(worksheet, "id", None),
        "title": getattr(worksheet, "title", None),
    }


def _sheet_source_identity(
    guild_id: int,
    credentials_info: Mapping[str, Any],
    *,
    worksheets: Optional[Sequence[Any]] = None,
    snapshot_rows: Optional[Sequence[Sequence[Sequence[Any]]]] = None,
) -> str:
    remote_identities = [_remote_worksheet_identity(worksheet) for worksheet in (worksheets or ())]
    has_stable_remote_ids = bool(remote_identities) and all(
        identity.get("spreadsheet_id") is not None and identity.get("worksheet_id") is not None
        for identity in remote_identities
    )
    if has_stable_remote_ids:
        identity = {
            "version": 3,
            "guild_id": guild_id,
            "worksheets": [
                {
                    "spreadsheet_id": item["spreadsheet_id"],
                    "worksheet_id": item["worksheet_id"],
                }
                for item in remote_identities
            ],
        }
    else:
        # Names are only a fallback for older clients/test doubles without
        # stable Google resource IDs. Credential rotation and worksheet renames
        # must not look like a newly linked physical Sheet when IDs are known.
        identity = {
            "version": 3,
            "configuration": _configured_sheet_identity(
                guild_id,
                credentials_info,
            ),
            "worksheets": remote_identities,
        }
    if snapshot_rows is not None:
        identity["snapshot"] = snapshot_rows
    return hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()


def _legacy_sheet_identity_v2(
    guild_id: int,
    credentials_info: Mapping[str, Any],
    worksheets: Sequence[Any],
) -> str:
    """Recognize projections adopted before stable physical-ID identity v3."""

    identity = {
        "configuration": _configured_sheet_identity(guild_id, credentials_info),
        "worksheets": [_remote_worksheet_identity(worksheet) for worksheet in worksheets],
    }
    return hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()


def _get_all_values(worksheet: Any, *, unformatted: bool = False) -> list[list[Any]]:
    """Read a worksheet with raw formula results when supported by gspread."""

    if not unformatted:
        return worksheet.get_all_values()
    try:
        return worksheet.get_all_values(value_render_option="UNFORMATTED_VALUE")
    except TypeError:
        # Small test doubles and older compatible gspread releases may not expose
        # the keyword. Integer parsing below remains defensive in that case.
        return worksheet.get_all_values()


def _record_issue(
    guild_id: int,
    source_reference: str,
    code: str,
    message: str,
    row: Sequence[Any],
) -> None:
    local_repository.record_migration_issue(
        guild_id=guild_id,
        source="google-sheet-bootstrap-v1",
        source_reference=source_reference,
        code=code,
        message=message,
        payload={"row": list(row)},
        deduplication_key=f"{guild_id}:{source_reference}:{code}:{_row_fingerprint(row)}",
    )


def _record_import_row(
    import_id: str,
    worksheet_kind: str,
    row_number: int,
    row: Sequence[Any],
    *,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
) -> None:
    local_repository.record_sheet_import_row(
        import_id,
        worksheet_kind,
        row_number,
        _row_fingerprint(row),
        list(row),
        entity_type=entity_type,
        entity_id=entity_id,
    )


def _import_players_rows(
    guild_id: int,
    import_id: str,
    rows: list[list[str]],
) -> int:
    imported = 0
    seen_discord_ids: set[int] = set()
    seen_nickname_keys: set[str] = set()
    for row_number, row in enumerate(rows[1:], start=2):
        if not any(_cell(row, index) for index in range(len(row))):
            continue
        reference = f"Players!{row_number}"
        discord_id_raw = _cell(row, 0)
        nickname = _cell(row, 1)
        membership_flag = _cell(row, 2).upper()
        try:
            discord_user_id = _parse_integer(discord_id_raw)
            if discord_user_id is None:
                raise ValueError
            if discord_user_id <= 0 or not nickname:
                raise ValueError
            if membership_flag not in {"YES", "NO"}:
                raise ValueError("membership flag must be YES or NO")
            silver = _parse_integer(_cell(row, 3))
            if silver is None or silver < 0:
                raise ValueError("Silver must be a non-negative integer")
        except (TypeError, ValueError) as error:
            _record_issue(
                guild_id,
                reference,
                "invalid_player_row",
                str(error) or "Invalid Discord ID, nickname, membership flag, or Silver.",
                row,
            )
            _record_import_row(import_id, "players", row_number, row, entity_type="issue")
            continue

        nickname_key = local_repository.normalize_nickname(nickname)
        if discord_user_id in seen_discord_ids or nickname_key in seen_nickname_keys:
            _record_issue(
                guild_id,
                reference,
                "duplicate_player_row",
                "The first valid Discord ID and nickname row won; this duplicate was quarantined.",
                row,
            )
            _record_import_row(
                import_id,
                "players",
                row_number,
                row,
                entity_type="issue",
            )
            continue
        seen_discord_ids.add(discord_user_id)
        seen_nickname_keys.add(nickname_key)

        siphon: Optional[int] = None
        raw_siphon = _cell(row, 4)
        if raw_siphon:
            try:
                siphon = _parse_integer(raw_siphon, allow_blank=True)
            except ValueError:
                _record_issue(
                    guild_id,
                    reference,
                    "invalid_siphon",
                    "Siphon was not an integer; the player was imported without it.",
                    row,
                )

        try:
            result = local_repository.import_player(
                guild_id,
                discord_user_id,
                nickname,
                is_active=membership_flag == "YES",
                silver=int(silver),
                siphon=siphon,
                siphon_synced_at=(datetime.now(timezone.utc) if siphon is not None else None),
            )
        except (OverflowError, ValueError) as error:
            _record_issue(
                guild_id,
                reference,
                "invalid_player_row",
                str(error),
                row,
            )
            _record_import_row(
                import_id,
                "players",
                row_number,
                row,
                entity_type="issue",
            )
            continue
        if result.status in {
            local_repository.PlayerImportStatus.NICKNAME_CONFLICT,
            local_repository.PlayerImportStatus.ALBION_ID_CONFLICT,
        }:
            _record_issue(
                guild_id,
                reference,
                result.status.value,
                "The first valid local/Sheet registration won; this conflicting row was quarantined.",
                row,
            )
            entity_type = "issue"
            entity_id = None
        else:
            imported += int(result.status == local_repository.PlayerImportStatus.IMPORTED)
            entity_type = "player"
            entity_id = f"{guild_id}:{discord_user_id}"
        _record_import_row(
            import_id,
            "players",
            row_number,
            row,
            entity_type=entity_type,
            entity_id=entity_id,
        )
    return imported


def _import_history_rows(
    guild_id: int,
    import_id: str,
    worksheet_kind: str,
    rows: list[list[str]],
    *,
    source_identity: str,
) -> int:
    """Preserve every legacy history row and normalize rows the repository can map."""

    imported = 0
    fingerprint_occurrences: dict[str, int] = {}
    for row_number, row in enumerate(rows[1:], start=2):
        if not any(_cell(row, index) for index in range(len(row))):
            continue
        reference = f"{worksheet_kind}!{row_number}"
        fingerprint = _row_fingerprint(row)
        occurrence = fingerprint_occurrences.get(fingerprint, 0) + 1
        fingerprint_occurrences[fingerprint] = occurrence
        stable_source_key = f"{source_identity}:{worksheet_kind}:{fingerprint}:{occurrence}"
        entity_type = "raw_history"
        entity_id: Optional[str] = None
        try:
            if worksheet_kind == "balance_history":
                amount = _parse_integer(_cell(row, 4))
                nickname = _cell(row, 3)
                if amount is None or not nickname:
                    raise ValueError("Missing nickname or amount")
                result = local_repository.import_balance_history(
                    guild_id,
                    nickname,
                    amount,
                    occurred_at=_cell(row, 0),
                    reason=_cell(row, 1),
                    actor_name=_cell(row, 2),
                    source_key=stable_source_key,
                )
                entity_id = result.event_id
                entity_type = "balance_history"
                imported += int(result.status == local_repository.HistoryImportStatus.IMPORTED)
            else:
                amount = _parse_integer(_cell(row, 6))
                nickname = _cell(row, 5)
                if amount is None or not nickname:
                    raise ValueError("Missing participant or amount")
                result = local_repository.import_lootsplit_history(
                    guild_id,
                    nickname,
                    amount,
                    battleboard_ids=tuple(
                        value.strip() for value in _cell(row, 0).split(",") if value.strip()
                    ),
                    occurred_at=_cell(row, 1),
                    actor_name=_cell(row, 2),
                    content_name=_cell(row, 3),
                    caller_name=_cell(row, 4),
                    source_key=stable_source_key,
                )
                entity_id = result.event_id
                entity_type = "lootsplit_history"
                imported += int(result.status == local_repository.HistoryImportStatus.IMPORTED)
            if result.player is None:
                _record_issue(
                    guild_id,
                    reference,
                    "history_player_not_found",
                    "The audit row was preserved, but its nickname did not match a local player.",
                    row,
                )
        except (OverflowError, TypeError, ValueError) as error:
            _record_issue(
                guild_id,
                reference,
                "unmapped_history_row",
                str(error),
                row,
            )
            entity_type = "raw_history"
            entity_id = None
        _record_import_row(
            import_id,
            worksheet_kind,
            row_number,
            row,
            entity_type=entity_type,
            entity_id=entity_id,
        )
    return imported


def _consume_staged_bootstrap_snapshot(
    ledger_id: int,
    staged_snapshot: local_repository.SheetImportSnapshot,
) -> tuple[local_repository.SheetImport, int]:
    """Apply one frozen snapshot locally and promote it in the same commit."""

    frozen_snapshot = staged_snapshot.snapshot
    players_rows = [list(row) for row in frozen_snapshot["players"]]
    lootsplit_rows = [list(row) for row in frozen_snapshot["lootsplit_history"]]
    balance_rows = [list(row) for row in frozen_snapshot["balance_history"]]
    import_metadata = dict(staged_snapshot.metadata)
    frozen_source_identity = str(
        import_metadata.get("logical_identity") or staged_snapshot.source_fingerprint
    )
    sheet_import = local_repository.begin_sheet_import(
        ledger_id,
        "google-bootstrap-v1",
        staged_snapshot.source_fingerprint,
        metadata=import_metadata,
    )
    imported = 0
    try:
        if sheet_import.status != local_repository.SheetImportStatus.COMPLETED:
            imported = _import_players_rows(
                ledger_id,
                sheet_import.import_id,
                players_rows,
            )
            imported += _import_history_rows(
                ledger_id,
                sheet_import.import_id,
                "balance_history",
                balance_rows,
                source_identity=frozen_source_identity,
            )
            imported += _import_history_rows(
                ledger_id,
                sheet_import.import_id,
                "lootsplit_history",
                lootsplit_rows,
                source_identity=frozen_source_identity,
            )
        completed = local_repository.complete_sheet_import(
            sheet_import.import_id,
            snapshot_id=staged_snapshot.snapshot_id,
        )
    except Exception as error:
        local_repository.fail_sheet_import(sheet_import.import_id, str(error))
        raise
    if completed is None:
        raise local_repository.RepositoryError("staged Sheet import disappeared before promotion")
    return completed, imported


def _bootstrap_guild_sync_unlocked(guild_id: int) -> SyncResult:
    """Import one legacy Sheet once, then adopt every linked Sheet local-first."""

    credentials_info = credential_store.get_credentials_info(guild_id)
    if not credentials_info:
        return SyncResult(False, "Google Sheet credentials are not linked or readable.")

    active_projection_id: Optional[str] = None
    imported = 0
    try:
        ledger = _active_ledger(guild_id, credentials_info)
        ledger_id = ledger.ledger_id
        completed_bootstrap = local_repository.get_latest_completed_sheet_import(
            ledger_id,
            "google-bootstrap-v1",
        )
        if completed_bootstrap is None:
            staged_snapshot = local_repository.get_staged_sheet_snapshot(
                ledger_id,
                "google-bootstrap-v1",
            )
            if staged_snapshot is not None:
                completed_bootstrap, imported = _consume_staged_bootstrap_snapshot(
                    ledger_id,
                    staged_snapshot,
                )

        resolved_worksheets = google_sheets.get_worksheets(guild_id)
        players_worksheet = resolved_worksheets[google_sheets.WORKSHEET_TYPE_PLAYERS]
        lootsplit_worksheet = resolved_worksheets[google_sheets.WORKSHEET_TYPE_LOOTSPLIT_HISTORY]
        balance_worksheet = resolved_worksheets[google_sheets.WORKSHEET_TYPE_BALANCE_HISTORY]
        worksheets = (
            players_worksheet,
            lootsplit_worksheet,
            balance_worksheet,
        )
        google_sheets.validate_players_headers(players_worksheet)
        google_sheets.ensure_lootsplit_history_headers(lootsplit_worksheet)
        google_sheets.ensure_balance_history_headers(balance_worksheet)

        logical_identity = _sheet_source_identity(
            guild_id,
            credentials_info,
            worksheets=worksheets,
        )
        compatible_legacy_identity = _legacy_sheet_identity_v2(
            guild_id,
            credentials_info,
            worksheets,
        )
        metadata = {
            **_configured_sheet_identity(guild_id, credentials_info),
            "logical_identity": logical_identity,
            "ledger_id": ledger_id,
            "ledger_generation": ledger.generation,
        }

        # A Sheet is a legacy source only for the first successful cutover of a
        # ledger generation. Relinking later is export-only, so Google can never
        # overwrite or duplicate the authoritative local ledger.
        if completed_bootstrap is None:
            players_rows = _get_all_values(players_worksheet)
            lootsplit_rows = _get_all_values(lootsplit_worksheet)
            balance_rows = _get_all_values(balance_worksheet)
            snapshot_payload = {
                "players": players_rows,
                "lootsplit_history": lootsplit_rows,
                "balance_history": balance_rows,
            }
            snapshot_fingerprint = _sheet_source_identity(
                guild_id,
                credentials_info,
                worksheets=worksheets,
                snapshot_rows=(players_rows, lootsplit_rows, balance_rows),
            )
            staged_snapshot = local_repository.stage_sheet_snapshot(
                ledger_id,
                "google-bootstrap-v1",
                snapshot_fingerprint,
                snapshot_payload,
                metadata=metadata,
            )
            completed_bootstrap, imported = _consume_staged_bootstrap_snapshot(
                ledger_id,
                staged_snapshot,
            )

        # Adopt every newly linked physical Sheet once: stamp all authoritative
        # local players with stable IDs/revisions and prepare idempotency columns.
        projection_import = local_repository.begin_sheet_import(
            ledger_id,
            "google-projection-v1",
            logical_identity,
            metadata=metadata,
        )
        active_projection_id = projection_import.import_id
        if projection_import.status != local_repository.SheetImportStatus.COMPLETED:
            _reconcile_player_projection(players_worksheet, ledger_id)
            _ensure_event_header(
                balance_worksheet,
                google_sheets.BALANCE_HISTORY_HEADERS,
                _BALANCE_EVENT_HEADER,
            )
            _ensure_event_header(
                lootsplit_worksheet,
                google_sheets.LOOTSPLIT_HISTORY_HEADERS,
                _LOOTSPLIT_EVENT_HEADER,
            )
            legacy_source_identity = str(
                (completed_bootstrap.metadata if completed_bootstrap else {}).get(
                    "logical_identity"
                )
                or ""
            )
            if legacy_source_identity and legacy_source_identity not in {
                logical_identity,
                compatible_legacy_identity,
            }:
                _reconcile_history_projection(
                    balance_worksheet,
                    lootsplit_worksheet,
                    ledger_id,
                )
            local_repository.complete_sheet_import(projection_import.import_id)
        active_projection_id = None
        return SyncResult(
            True,
            "Google Sheet cutover and local projection are ready.",
            imported_rows=imported,
        )
    except Exception as error:
        if active_projection_id is not None:
            local_repository.fail_sheet_import(active_projection_id, str(error))
        LOGGER.exception("Google bootstrap failed for guild %s", guild_id)
        return SyncResult(False, str(error))


def bootstrap_guild_sync(guild_id: int) -> SyncResult:
    with _sync_lock(guild_id):
        return _bootstrap_guild_sync_unlocked(guild_id)


async def bootstrap_guild(guild_id: int) -> SyncResult:
    return await external_io.run_google(bootstrap_guild_sync, guild_id)


def bootstrap_all_linked_sheets() -> dict[int, SyncResult]:
    results: dict[int, SyncResult] = {}
    for raw_guild_id, link in document_store.load_google_sheet_links().items():
        if not isinstance(link, Mapping):
            continue
        if str(link.get("status") or "active").strip().casefold() != "active":
            continue
        if link.get("disabled") is True or link.get("quarantined") is True:
            continue
        try:
            guild_id = int(raw_guild_id)
        except (TypeError, ValueError):
            continue
        results[guild_id] = bootstrap_guild_sync(guild_id)
    return results


def is_cutover_ready(guild_id: int) -> bool:
    """Allow local ledger workflows unless a linked legacy import is unfinished."""

    try:
        link = document_store.get_google_sheet_link(guild_id)
        if link is None:
            return True
        if not isinstance(link, Mapping):
            raise TypeError("Google Sheet link metadata must be an object.")
        if str(link.get("status") or "active").strip().casefold() in {
            "disabled",
            "quarantined",
        }:
            return True
        if str(link.get("status") or "active").strip().casefold() != "active":
            return False
        if link.get("disabled") is True or link.get("quarantined") is True:
            return True
        ledger = _active_ledger(int(guild_id), link)
        return local_repository.has_completed_sheet_import(
            ledger.ledger_id,
            "google-bootstrap-v1",
        )
    except Exception:
        LOGGER.exception("Could not determine Sheet cutover state for guild %s", guild_id)
        return False


def _ensure_players_projection_headers(worksheet) -> None:
    google_sheets.validate_players_headers(worksheet)
    existing = list(worksheet.row_values(1))
    padded = existing + [""] * max(0, len(_PLAYERS_PROJECTION_HEADERS) - len(existing))
    for index, expected in enumerate(_PLAYERS_PROJECTION_HEADERS[4:], start=4):
        actual = str(padded[index] or "").strip()
        if actual not in {"", expected}:
            raise google_sheets.WorksheetSchemaError(
                f"Players column {index + 1} must be {expected}."
            )
    if padded[5:7] != _PLAYERS_PROJECTION_HEADERS[5:7]:
        # E1 may contain the Sheet-owned array formula that emits the Siphon
        # heading and values. It is never part of a bot-authored update.
        worksheet.update(
            range_name="F1:G1",
            values=[_PLAYERS_PROJECTION_HEADERS[5:]],
            value_input_option="RAW",
        )


def _ensure_event_header(worksheet, base_headers: Sequence[str], event_header: str) -> None:
    first_row = list(worksheet.row_values(1))
    actual_base = [str(value or "").strip() for value in first_row[: len(base_headers)]]
    if actual_base != list(base_headers):
        raise google_sheets.WorksheetSchemaError("History worksheet headers do not match.")
    existing_event_header = _cell(first_row, len(base_headers))
    if existing_event_header not in {"", event_header}:
        raise google_sheets.WorksheetSchemaError(f"History event column must be {event_header}.")
    if existing_event_header != event_header:
        column_letter = chr(ord("A") + len(base_headers))
        worksheet.update(
            range_name=f"{column_letter}1:{column_letter}1",
            values=[[event_header]],
            value_input_option="RAW",
        )


class _PlayerProjectionRows:
    """One per-sync, O(1) index over the managed Players worksheet rows."""

    def __init__(self, rows: list[list[Any]]) -> None:
        self.rows = rows
        self.by_registration_id: dict[str, int] = {}
        self.by_discord_id: dict[str, int] = {}
        self.empty_rows: deque[int] = deque()
        self.owner_by_row: dict[int, str] = {}
        for row_index, row in enumerate(rows[1:], start=2):
            registration_id = _cell(row, 5)
            discord_id = _cell(row, 0)
            if registration_id:
                self.by_registration_id.setdefault(registration_id, row_index)
            if discord_id:
                self.by_discord_id.setdefault(discord_id, row_index)
            if not any(_cell(row, index) for index in range(min(7, len(row)))):
                self.empty_rows.append(row_index)

    def resolve(self, player: Mapping[str, Any]) -> int:
        registration_id = f"{player['guild_id']}:{player['discord_user_id']}"
        discord_id = str(player["discord_user_id"])
        for row_index in (
            self.by_registration_id.get(registration_id),
            self.by_discord_id.get(discord_id),
        ):
            if row_index is None:
                continue
            owner = self.owner_by_row.get(row_index)
            if owner is None or owner == registration_id:
                self.owner_by_row[row_index] = registration_id
                return row_index
        while self.empty_rows:
            row_index = self.empty_rows.popleft()
            if row_index not in self.owner_by_row:
                self.owner_by_row[row_index] = registration_id
                return row_index
        row_index = max(2, len(self.rows) + 1)
        self.owner_by_row[row_index] = registration_id
        return row_index

    def record(self, row_index: int, player: Mapping[str, Any]) -> None:
        registration_id = f"{player['guild_id']}:{player['discord_user_id']}"
        discord_id = str(player["discord_user_id"])
        while len(self.rows) < row_index:
            self.rows.append([])
        cached = list(self.rows[row_index - 1])
        old_registration_id = _cell(cached, 5)
        old_discord_id = _cell(cached, 0)
        if self.by_registration_id.get(old_registration_id) == row_index:
            self.by_registration_id.pop(old_registration_id, None)
        if self.by_discord_id.get(old_discord_id) == row_index:
            self.by_discord_id.pop(old_discord_id, None)
        cached.extend([""] * max(0, 7 - len(cached)))
        cached[0:4] = [
            discord_id,
            str(player["nickname"]),
            "YES" if bool(player["is_in_guild"]) else "NO",
            str(player["silver"]),
        ]
        cached[5:7] = [registration_id, str(player["revision"])]
        self.rows[row_index - 1] = cached
        self.by_registration_id[registration_id] = row_index
        self.by_discord_id[discord_id] = row_index
        self.owner_by_row[row_index] = registration_id


def _project_player(
    worksheet,
    player: Mapping[str, Any],
    *,
    row_cache: Optional[_PlayerProjectionRows] = None,
    ensure_headers: bool = True,
) -> None:
    if ensure_headers:
        _ensure_players_projection_headers(worksheet)
    if row_cache is None:
        row_cache = _PlayerProjectionRows(worksheet.get_all_values())
    row_index = row_cache.resolve(player)
    local_id = f"{player['guild_id']}:{player['discord_user_id']}"
    worksheet.batch_update(
        [
            {
                "range": f"A{row_index}:D{row_index}",
                "values": [
                    [
                        str(player["discord_user_id"]),
                        str(player["nickname"]),
                        "YES" if bool(player["is_in_guild"]) else "NO",
                        str(player["silver"]),
                    ]
                ],
            },
            {
                "range": f"F{row_index}:G{row_index}",
                "values": [[local_id, str(player["revision"])]],
            },
        ],
        value_input_option="RAW",
    )
    row_cache.record(row_index, player)


def _reconcile_player_projection(worksheet: Any, guild_id: int) -> None:
    """Seed every local player and clear non-authoritative managed Sheet rows."""

    players = local_repository.list_players(guild_id)
    _ensure_players_projection_headers(worksheet)
    rows = _get_all_values(worksheet)
    row_cache = _PlayerProjectionRows(rows)
    for player in players:
        _project_player(
            worksheet,
            {
                "guild_id": player.guild_id,
                "discord_user_id": player.discord_user_id,
                "nickname": player.nickname,
                "is_in_guild": player.is_active,
                "silver": player.silver,
                "revision": player.revision,
            },
            row_cache=row_cache,
            ensure_headers=False,
        )

    local_ids = {player.discord_user_id for player in players}
    seen_local_ids: set[int] = set()
    clear_requests: list[dict[str, Any]] = []
    # Reconciliation may reuse a row that originally had only a Discord ID or
    # otherwise stale managed fields. Inspect the updated in-memory view so we
    # do not immediately clear a row that `_project_player` just repaired.
    for row_index, row in enumerate(row_cache.rows[1:], start=2):
        raw_discord_id = _cell(row, 0)
        discord_user_id = int(raw_discord_id) if raw_discord_id.isdigit() else None
        expected_registration_id = (
            f"{guild_id}:{discord_user_id}" if discord_user_id is not None else ""
        )
        is_authoritative = (
            discord_user_id is not None
            and discord_user_id in local_ids
            and discord_user_id not in seen_local_ids
            and _cell(row, 5) == expected_registration_id
        )
        if is_authoritative and discord_user_id is not None:
            seen_local_ids.add(discord_user_id)
            continue
        if not any(_cell(row, index) for index in (0, 1, 2, 3, 5, 6)):
            continue
        clear_requests.extend(
            [
                {
                    "range": f"A{row_index}:D{row_index}",
                    "values": [["", "", "", ""]],
                },
                {
                    "range": f"F{row_index}:G{row_index}",
                    "values": [["", ""]],
                },
            ]
        )
    if clear_requests:
        worksheet.batch_update(clear_requests, value_input_option="RAW")


def _sheet_date(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return datetime.now(timezone.utc).strftime("%m/%d/%y %H:%M UTC")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%m/%d/%y %H:%M UTC")


def _append_balance_projection(
    worksheet,
    payload: Mapping[str, Any],
    *,
    existing_event_ids: Optional[set[str]] = None,
    headers_ready: bool = False,
) -> None:
    if not headers_ready:
        _ensure_event_header(
            worksheet,
            google_sheets.BALANCE_HISTORY_HEADERS,
            _BALANCE_EVENT_HEADER,
        )
    event_id = str(payload["event_id"])
    if existing_event_ids is None:
        existing_event_ids = {str(value or "").strip() for value in worksheet.col_values(6)}
    if event_id in existing_event_ids:
        return
    player = payload["player"]
    worksheet.append_row(
        [
            _sheet_date(payload.get("occurred_at")),
            str(payload.get("reason") or ""),
            str(payload.get("actor_name") or ""),
            str(player["nickname"]),
            str(payload["actual_delta"]),
            event_id,
        ],
        value_input_option="RAW",
    )
    existing_event_ids.add(event_id)


def _append_lootsplit_projections(
    lootsplit_worksheet,
    balance_worksheet,
    payload,
    *,
    lootsplit_ids: Optional[set[str]] = None,
    balance_ids: Optional[set[str]] = None,
    headers_ready: bool = False,
) -> None:
    if not headers_ready:
        _ensure_event_header(
            lootsplit_worksheet,
            google_sheets.LOOTSPLIT_HISTORY_HEADERS,
            _LOOTSPLIT_EVENT_HEADER,
        )
        _ensure_event_header(
            balance_worksheet,
            google_sheets.BALANCE_HISTORY_HEADERS,
            _BALANCE_EVENT_HEADER,
        )
    if lootsplit_ids is None:
        lootsplit_ids = {str(value or "").strip() for value in lootsplit_worksheet.col_values(8)}
    if balance_ids is None:
        balance_ids = {str(value or "").strip() for value in balance_worksheet.col_values(6)}
    lootsplit_rows = []
    balance_rows = []
    battleboards = ",".join(str(value) for value in payload.get("battleboard_ids", ()))
    operational_officer = str(payload.get("officer_name") or payload.get("actor_name") or "")
    for credit in payload.get("credits", ()):
        event_id = str(credit["history_event_id"])
        if event_id not in lootsplit_ids:
            lootsplit_rows.append(
                [
                    battleboards,
                    _sheet_date(payload.get("occurred_at")),
                    operational_officer,
                    str(payload.get("content_name") or ""),
                    str(payload.get("caller_name") or ""),
                    str(credit["nickname"]),
                    str(credit["amount"]),
                    event_id,
                ]
            )
        if event_id not in balance_ids:
            balance_rows.append(
                [
                    _sheet_date(payload.get("occurred_at")),
                    "Lootsplit",
                    operational_officer,
                    str(credit["nickname"]),
                    str(credit["amount"]),
                    event_id,
                ]
            )
    if lootsplit_rows:
        lootsplit_worksheet.append_rows(lootsplit_rows, value_input_option="RAW")
        lootsplit_ids.update(str(row[7]) for row in lootsplit_rows)
    if balance_rows:
        balance_worksheet.append_rows(balance_rows, value_input_option="RAW")
        balance_ids.update(str(row[5]) for row in balance_rows)


def _reconcile_history_projection(
    balance_worksheet: Any,
    lootsplit_worksheet: Any,
    ledger_id: int,
    *,
    include_imported: bool = True,
) -> None:
    """Seed a newly linked physical Sheet from SQLite's immutable audit log."""

    _ensure_event_header(
        balance_worksheet,
        google_sheets.BALANCE_HISTORY_HEADERS,
        _BALANCE_EVENT_HEADER,
    )
    _ensure_event_header(
        lootsplit_worksheet,
        google_sheets.LOOTSPLIT_HISTORY_HEADERS,
        _LOOTSPLIT_EVENT_HEADER,
    )
    existing_balance_ids = {str(value or "").strip() for value in balance_worksheet.col_values(6)}
    balance_rows: list[list[str]] = []
    for balance_record in local_repository.iter_balance_history(ledger_id):
        if not include_imported and balance_record["event_kind"] == "sheet_import":
            continue
        event_id = str(balance_record["event_id"])
        if event_id in existing_balance_ids:
            continue
        balance_rows.append(
            [
                _sheet_date(balance_record["occurred_at"]),
                str(balance_record["reason"] or ""),
                str(balance_record["actor_name"] or ""),
                str(balance_record["nickname_snapshot"]),
                str(balance_record["actual_delta"]),
                event_id,
            ]
        )
        existing_balance_ids.add(event_id)
    if balance_rows:
        balance_worksheet.append_rows(balance_rows, value_input_option="RAW")

    existing_lootsplit_ids = {
        str(value or "").strip() for value in lootsplit_worksheet.col_values(8)
    }
    lootsplit_rows: list[list[str]] = []
    for lootsplit_record in local_repository.iter_lootsplit_history(ledger_id):
        if not include_imported and lootsplit_record["event_kind"] == "sheet_import":
            continue
        event_id = str(lootsplit_record["event_id"])
        if event_id in existing_lootsplit_ids:
            continue
        lootsplit_rows.append(
            [
                ",".join(str(value) for value in lootsplit_record["battleboard_ids"]),
                _sheet_date(lootsplit_record["occurred_at"]),
                str(lootsplit_record.get("officer_name") or lootsplit_record["actor_name"] or ""),
                str(lootsplit_record["content_name"] or ""),
                str(lootsplit_record["caller_name"] or ""),
                str(lootsplit_record["nickname"]),
                str(lootsplit_record["amount"]),
                event_id,
            ]
        )
        existing_lootsplit_ids.add(event_id)
    if lootsplit_rows:
        lootsplit_worksheet.append_rows(lootsplit_rows, value_input_option="RAW")


def _project_outbox_event(
    event: local_repository.OutboxEvent,
    worksheets: Mapping[str, Any],
    player_rows: _PlayerProjectionRows,
    balance_event_ids: set[str],
    lootsplit_event_ids: set[str],
) -> None:
    players_worksheet = worksheets[google_sheets.WORKSHEET_TYPE_PLAYERS]
    payload = event.payload
    if event.event_type == "player.upsert":
        player_id = int(payload["player"]["discord_user_id"])
        current = local_repository.get_player(event.guild_id, player_id)
        if current is None:
            raise ValueError("The local player for this projection no longer exists.")
        _project_player(
            players_worksheet,
            {
                "guild_id": current.guild_id,
                "discord_user_id": current.discord_user_id,
                "nickname": current.nickname,
                "is_in_guild": current.is_active,
                "silver": current.silver,
                "revision": current.revision,
            },
            row_cache=player_rows,
            ensure_headers=False,
        )
        return
    if event.event_type == "balance.changed":
        player_id = int(payload["player"]["discord_user_id"])
        current = local_repository.get_player(event.guild_id, player_id)
        if current is None:
            raise ValueError("The local player for this projection no longer exists.")
        _project_player(
            players_worksheet,
            {
                "guild_id": current.guild_id,
                "discord_user_id": current.discord_user_id,
                "nickname": current.nickname,
                "is_in_guild": current.is_active,
                "silver": current.silver,
                "revision": current.revision,
            },
            row_cache=player_rows,
            ensure_headers=False,
        )
        balance_worksheet = worksheets[google_sheets.WORKSHEET_TYPE_BALANCE_HISTORY]
        _append_balance_projection(
            balance_worksheet,
            payload,
            existing_event_ids=balance_event_ids,
            headers_ready=True,
        )
        return
    if event.event_type == "lootsplit.applied":
        for credit in payload.get("credits", ()):
            player = local_repository.get_player(
                event.guild_id,
                int(credit["discord_user_id"]),
            )
            if player is not None:
                _project_player(
                    players_worksheet,
                    {
                        "guild_id": player.guild_id,
                        "discord_user_id": player.discord_user_id,
                        "nickname": player.nickname,
                        "is_in_guild": player.is_active,
                        "silver": player.silver,
                        "revision": player.revision,
                    },
                    row_cache=player_rows,
                    ensure_headers=False,
                )
        lootsplit_worksheet = worksheets[google_sheets.WORKSHEET_TYPE_LOOTSPLIT_HISTORY]
        balance_worksheet = worksheets[google_sheets.WORKSHEET_TYPE_BALANCE_HISTORY]
        _append_lootsplit_projections(
            lootsplit_worksheet,
            balance_worksheet,
            payload,
            lootsplit_ids=lootsplit_event_ids,
            balance_ids=balance_event_ids,
            headers_ready=True,
        )
        return
    raise ValueError(f"Unsupported Google outbox event: {event.event_type}")


def _flush_outbox_sync_unlocked(guild_id: int, *, limit: int = 100) -> SyncResult:
    credentials_info = credential_store.get_credentials_info(guild_id)
    if not credentials_info:
        return SyncResult(False, "Google Sheet credentials are not linked or readable.")
    try:
        ledger_id = _active_ledger(guild_id, credentials_info).ledger_id
    except Exception as error:
        return SyncResult(False, str(error))
    worker_id = f"realm-protector-{uuid4()}"
    processed = 0
    worksheets: Optional[dict[str, Any]] = None
    player_rows: Optional[_PlayerProjectionRows] = None
    balance_event_ids: set[str] = set()
    lootsplit_event_ids: set[str] = set()
    while processed < limit:
        events = local_repository.claim_pending_outbox(
            worker_id,
            guild_id=ledger_id,
            limit=1,
            lease_seconds=600,
        )
        if not events:
            if local_repository.has_incomplete_outbox(guild_id=ledger_id):
                return SyncResult(
                    False,
                    "Google projection is pending an earlier event retry.",
                    processed_events=processed,
                )
            if local_repository.has_dead_letter_outbox(guild_id=ledger_id):
                return SyncResult(
                    False,
                    "Google projection has quarantined events that require retry or dismissal.",
                    processed_events=processed,
                    incomplete=True,
                )
            return SyncResult(
                True,
                "Google projection is current.",
                processed_events=processed,
            )
        event = events[0]
        try:
            if worksheets is None:
                worksheets = google_sheets.get_worksheets(guild_id)
                players_worksheet = worksheets[google_sheets.WORKSHEET_TYPE_PLAYERS]
                balance_worksheet = worksheets[google_sheets.WORKSHEET_TYPE_BALANCE_HISTORY]
                lootsplit_worksheet = worksheets[google_sheets.WORKSHEET_TYPE_LOOTSPLIT_HISTORY]
                _ensure_players_projection_headers(players_worksheet)
                _ensure_event_header(
                    balance_worksheet,
                    google_sheets.BALANCE_HISTORY_HEADERS,
                    _BALANCE_EVENT_HEADER,
                )
                _ensure_event_header(
                    lootsplit_worksheet,
                    google_sheets.LOOTSPLIT_HISTORY_HEADERS,
                    _LOOTSPLIT_EVENT_HEADER,
                )
                player_rows = _PlayerProjectionRows(_get_all_values(players_worksheet))
                balance_event_ids = {
                    str(value or "").strip() for value in balance_worksheet.col_values(6)
                }
                lootsplit_event_ids = {
                    str(value or "").strip() for value in lootsplit_worksheet.col_values(8)
                }
            assert worksheets is not None and player_rows is not None
            _project_outbox_event(
                event,
                worksheets,
                player_rows,
                balance_event_ids,
                lootsplit_event_ids,
            )
        except Exception as error:
            if event.attempts + 1 >= MAX_OUTBOX_ATTEMPTS:
                local_repository.dead_letter_outbox(
                    event.event_id,
                    str(error),
                    worker_id=worker_id,
                )
                failure_message = (
                    f"{error} The event exceeded {MAX_OUTBOX_ATTEMPTS} attempts "
                    "and was quarantined for operator recovery."
                )
            else:
                retry_seconds = min(3600, 30 * (2 ** min(event.attempts, 7)))
                local_repository.fail_outbox(
                    event.event_id,
                    str(error),
                    retry_after_seconds=retry_seconds,
                    worker_id=worker_id,
                )
                failure_message = str(error)
            LOGGER.exception(
                "Google projection failed for guild %s event %s",
                guild_id,
                event.event_id,
            )
            return SyncResult(
                False,
                failure_message,
                processed_events=processed,
                incomplete=True,
            )
        if not local_repository.ack_outbox(event.event_id, worker_id=worker_id):
            return SyncResult(
                False,
                "Google event lease expired before it could be acknowledged.",
                processed_events=processed,
            )
        processed += 1
    if local_repository.has_incomplete_outbox(guild_id=ledger_id):
        return SyncResult(
            False,
            "Google projection batch limit reached; more events remain pending.",
            processed_events=processed,
        )
    if local_repository.has_dead_letter_outbox(guild_id=ledger_id):
        return SyncResult(
            False,
            "Google projection completed, but quarantined events require recovery.",
            processed_events=processed,
            incomplete=True,
        )
    return SyncResult(
        True,
        "Google projection batch completed.",
        processed_events=processed,
    )


def flush_outbox_sync(guild_id: int, *, limit: int = 100) -> SyncResult:
    with _sync_lock(guild_id):
        return _flush_outbox_sync_unlocked(guild_id, limit=limit)


async def flush_outbox(guild_id: int, *, limit: int = 100) -> SyncResult:
    return await external_io.run_google(flush_outbox_sync, guild_id, limit=limit)


def _rebuild_projection_sync_unlocked(guild_id: int) -> SyncResult:
    """Reconcile every bot-owned Google projection field from authoritative SQLite.

    The rebuild is deliberately independent of outbox acknowledgement. Pending
    events remain queued and are safe to replay because player writes and
    history appends are idempotent by stable local/event identifiers.
    """

    credentials_info = credential_store.get_credentials_info(guild_id)
    if not credentials_info:
        return SyncResult(
            False,
            "Google Sheet credentials are not linked or readable.",
            incomplete=True,
        )
    try:
        ledger = _active_ledger(guild_id, credentials_info)
        completed_bootstrap = local_repository.get_latest_completed_sheet_import(
            ledger.ledger_id,
            "google-bootstrap-v1",
        )
        if completed_bootstrap is None:
            return SyncResult(
                False,
                "Google Sheet cutover must complete before rebuilding its projection.",
                incomplete=True,
            )
        worksheets = google_sheets.get_worksheets(guild_id)
        players_worksheet = worksheets[google_sheets.WORKSHEET_TYPE_PLAYERS]
        balance_worksheet = worksheets[google_sheets.WORKSHEET_TYPE_BALANCE_HISTORY]
        lootsplit_worksheet = worksheets[google_sheets.WORKSHEET_TYPE_LOOTSPLIT_HISTORY]
        worksheet_sequence = (
            players_worksheet,
            lootsplit_worksheet,
            balance_worksheet,
        )
        current_identity = _sheet_source_identity(
            guild_id,
            credentials_info,
            worksheets=worksheet_sequence,
        )
        compatible_legacy_identity = _legacy_sheet_identity_v2(
            guild_id,
            credentials_info,
            worksheet_sequence,
        )
        bootstrap_identity = str(completed_bootstrap.metadata.get("logical_identity") or "")
        include_imported_history = bool(
            bootstrap_identity
            and bootstrap_identity not in {current_identity, compatible_legacy_identity}
        )
        _reconcile_player_projection(players_worksheet, ledger.ledger_id)
        _reconcile_history_projection(
            balance_worksheet,
            lootsplit_worksheet,
            ledger.ledger_id,
            include_imported=include_imported_history,
        )
    except Exception as error:
        LOGGER.exception("Google projection rebuild failed for guild %s", guild_id)
        return SyncResult(False, str(error), incomplete=True)
    return SyncResult(
        True,
        "Google projection was rebuilt from authoritative local SQLite data.",
    )


def rebuild_projection_sync(guild_id: int) -> SyncResult:
    """Synchronously rebuild one Discord guild's optional Google projection."""

    with _sync_lock(guild_id):
        return _rebuild_projection_sync_unlocked(guild_id)


async def rebuild_projection(guild_id: int) -> SyncResult:
    """Rebuild one projection without blocking the Discord event loop."""

    return await external_io.run_google(rebuild_projection_sync, guild_id)


def _refresh_siphon_sync_unlocked(
    guild_id: int,
    *,
    flush_pending: bool = True,
) -> SyncResult:
    processed_events = 0
    if flush_pending:
        projection = _flush_outbox_sync_unlocked(guild_id)
        if not projection.success:
            return projection
        processed_events = projection.processed_events
    credentials_info = credential_store.get_credentials_info(guild_id)
    if not credentials_info:
        return SyncResult(False, "Google Sheet credentials are not linked or readable.")

    ledger_id = _active_ledger(guild_id, credentials_info).ledger_id

    worksheet = google_sheets.get_worksheets(
        guild_id,
        (google_sheets.WORKSHEET_TYPE_PLAYERS,),
    )[google_sheets.WORKSHEET_TYPE_PLAYERS]
    _ensure_players_projection_headers(worksheet)
    rows = _get_all_values(worksheet, unformatted=True)
    expected_players = local_repository.list_active_players(ledger_id)
    siphon_updates: list[local_repository.SiphonUpdate] = []
    rows_by_discord_id: dict[int, set[int]] = {}
    rows_by_registration_id: dict[str, set[int]] = {}
    for row_index, row in enumerate(rows[1:], start=1):
        raw_discord_id = _cell(row, 0)
        try:
            parsed_discord_id = _parse_integer(raw_discord_id, allow_blank=True)
        except ValueError:
            parsed_discord_id = None
        if parsed_discord_id is not None and parsed_discord_id > 0:
            rows_by_discord_id.setdefault(parsed_discord_id, set()).add(row_index)
        registration_id = _cell(row, 5)
        if registration_id:
            rows_by_registration_id.setdefault(registration_id, set()).add(row_index)

    for player in expected_players:
        registration_id = f"{ledger_id}:{player.discord_user_id}"
        candidate_indices = set(rows_by_discord_id.get(player.discord_user_id, set()))
        candidate_indices.update(rows_by_registration_id.get(registration_id, set()))
        if len(candidate_indices) != 1:
            continue
        row = rows[next(iter(candidate_indices))]
        if (
            _cell(row, 0) != str(player.discord_user_id)
            or _cell(row, 2).upper() != "YES"
            or _cell(row, 5) != registration_id
        ):
            continue
        try:
            sheet_silver = _parse_integer(_cell(row, 3))
            siphon = _parse_integer(_cell(row, 4), allow_blank=True)
            expected_revision = _parse_integer(_cell(row, 6), allow_blank=True)
        except (TypeError, ValueError):
            continue
        if siphon is None or expected_revision is None:
            continue
        if sheet_silver != player.silver:
            continue
        if expected_revision != player.revision:
            continue
        siphon_updates.append(
            local_repository.SiphonUpdate(
                discord_user_id=player.discord_user_id,
                siphon=siphon,
                expected_revision=expected_revision,
            )
        )
    cache_results = local_repository.cache_siphons(
        ledger_id,
        siphon_updates,
        replace_snapshot=True,
    )
    updated = sum(
        result.status == local_repository.SiphonCacheStatus.UPDATED for result in cache_results
    )
    expected = len(expected_players)
    rejected = expected - updated
    complete = rejected == 0
    return SyncResult(
        complete,
        (
            "Siphon refreshed from Google Sheets."
            if complete
            else (
                "Siphon refresh is incomplete: "
                f"{rejected} of {expected} active player rows were missing, "
                "duplicated, stale, or invalid. Their cached Siphon values were cleared."
            )
        ),
        processed_events=processed_events,
        updated_siphon_rows=updated,
        expected_siphon_rows=expected,
        rejected_siphon_rows=rejected,
        incomplete=not complete,
    )


def refresh_siphon_sync(guild_id: int, *, flush_pending: bool = True) -> SyncResult:
    with _sync_lock(guild_id):
        return _refresh_siphon_sync_unlocked(
            guild_id,
            flush_pending=flush_pending,
        )


async def refresh_siphon(
    guild_id: int,
    *,
    flush_pending: bool = True,
) -> SyncResult:
    return await external_io.run_google(
        refresh_siphon_sync,
        guild_id,
        flush_pending=flush_pending,
    )


async def _sync_loop() -> None:
    while True:
        try:
            for raw_guild_id, link in document_store.load_google_sheet_links().items():
                if not isinstance(link, Mapping):
                    continue
                if str(link.get("status") or "active").strip().casefold() != "active":
                    continue
                if link.get("disabled") is True or link.get("quarantined") is True:
                    continue
                try:
                    guild_id = int(raw_guild_id)
                except (TypeError, ValueError):
                    continue
                try:
                    bootstrap = await bootstrap_guild(guild_id)
                    if bootstrap.success:
                        await refresh_siphon(guild_id, flush_pending=True)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOGGER.exception(
                        "Google synchronization failed for guild %s",
                        guild_id,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Google synchronization tick failed")
        await asyncio.sleep(SYNC_INTERVAL_SECONDS)


def start_google_sync() -> None:
    global _sync_task
    if _sync_task is not None and not _sync_task.done():
        return
    _sync_task = asyncio.create_task(
        _sync_loop(),
        name="realm-protector-google-sync",
    )


async def stop_google_sync() -> None:
    """Stop the background Google worker and release its module-level handle."""

    global _sync_task
    task = _sync_task
    if task is None:
        return
    _sync_task = None
    if task is asyncio.current_task():
        task.cancel()
        return
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        LOGGER.exception("Google synchronization worker failed during shutdown")


__all__ = [
    "MAX_OUTBOX_ATTEMPTS",
    "SIPHON_STALE_AFTER_SECONDS",
    "SYNC_INTERVAL_SECONDS",
    "SyncResult",
    "bootstrap_all_linked_sheets",
    "bootstrap_guild",
    "bootstrap_guild_sync",
    "flush_outbox",
    "flush_outbox_sync",
    "is_cutover_ready",
    "rebuild_projection",
    "rebuild_projection_sync",
    "refresh_siphon",
    "refresh_siphon_sync",
    "start_google_sync",
    "stop_google_sync",
]
