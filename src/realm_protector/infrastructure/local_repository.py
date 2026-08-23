"""Authoritative local player, economy, and synchronization persistence.

The repository deliberately contains no Discord or Google client code.  Commands
write here first; Google Sheets is updated later from the transactional outbox.
All timestamps are UTC ISO-8601 strings and all public result objects are
immutable so callers cannot accidentally mutate persisted state.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence, Union
from uuid import UUID, uuid4, uuid5

from src.realm_protector.infrastructure import sqlite_database

DatabasePath = Optional[Union[str, Path]]
_EVENT_NAMESPACE = UUID("7f291cc7-f3ad-5c2c-9b2b-e6486b8f82d1")
_SCHEMA_INITIALIZATION_LOCK = threading.RLock()
_SCHEMA_READY_FILES: dict[Path, tuple[int, int]] = {}
SQLITE_INTEGER_MIN = -(1 << 63)
SQLITE_INTEGER_MAX = (1 << 63) - 1


class RepositoryError(RuntimeError):
    """Base class for local repository failures."""


class RepositoryCorruptionError(RepositoryError):
    """Raised when a persisted repository JSON value cannot be decoded."""


class IdempotencyConflictError(RepositoryError):
    """Raised when an idempotency key is reused for a different request."""


class LedgerNotActiveError(RepositoryError):
    """Raised when a business mutation targets a missing or archived ledger."""


class TargetGuildConflictError(RepositoryError):
    """Raised when an Albion guild is already active on another Discord server."""


class RegistrationStatus(str, Enum):
    CREATED = "created"
    REACTIVATED = "reactivated"
    ALREADY_REGISTERED = "already_registered"
    NICKNAME_CONFLICT = "nickname_conflict"
    ALBION_ID_CONFLICT = "albion_id_conflict"


class PlayerImportStatus(str, Enum):
    IMPORTED = "imported"
    LOCAL_PRESERVED = "local_preserved"
    NICKNAME_CONFLICT = "nickname_conflict"
    ALBION_ID_CONFLICT = "albion_id_conflict"


class SiphonCacheStatus(str, Enum):
    UPDATED = "updated"
    NOT_FOUND = "not_found"
    STALE_REVISION = "stale_revision"


class SheetImportStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class SheetSnapshotStatus(str, Enum):
    STAGED = "staged"
    APPLIED = "applied"


class HistoryImportStatus(str, Enum):
    IMPORTED = "imported"
    ALREADY_IMPORTED = "already_imported"


@dataclass(frozen=True)
class PlayerRecord:
    guild_id: int
    discord_user_id: int
    nickname: str
    nickname_key: str
    albion_player_id: Optional[str]
    is_active: bool
    silver: int
    all_time_earnings: int
    revision: int
    siphon: Optional[int]
    siphon_revision: Optional[int]
    siphon_synced_at: Optional[str]
    created_at: str
    updated_at: str

    @property
    def active(self) -> bool:
        """Compatibility/readability alias for membership workflows."""

        return self.is_active

    @property
    def is_in_guild(self) -> bool:
        """Expose the name used by the legacy Google worksheet."""

        return self.is_active


@dataclass(frozen=True)
class RegistrationResult:
    status: RegistrationStatus
    player: Optional[PlayerRecord]
    conflicting_discord_user_id: Optional[int] = None


@dataclass(frozen=True)
class PlayerImportResult:
    status: PlayerImportStatus
    player: Optional[PlayerRecord]
    conflicting_discord_user_id: Optional[int] = None


@dataclass(frozen=True)
class BalanceSnapshot:
    guild_id: int
    discord_user_id: int
    nickname: str
    silver: int
    all_time_earnings: int
    revision: int
    siphon: Optional[int]
    siphon_revision: Optional[int]
    siphon_synced_at: Optional[str]
    is_active: bool


@dataclass(frozen=True)
class BalanceChangeResult:
    event_id: str
    player: PlayerRecord
    previous_balance: int
    requested_delta: int
    actual_delta: int
    updated_balance: int
    idempotent_replay: bool = False


@dataclass(frozen=True)
class SilverLeaderboardPage:
    players: tuple[PlayerRecord, ...]
    total_players: int
    limit: int
    offset: int


@dataclass(frozen=True)
class LootsplitCredit:
    discord_user_id: int
    nickname: str
    amount: int
    previous_balance: int
    updated_balance: int
    player_revision: int
    history_event_id: str


@dataclass(frozen=True)
class LootsplitResult:
    lootsplit_id: str
    credits: tuple[LootsplitCredit, ...]
    missing_nicknames: tuple[str, ...]
    idempotent_replay: bool = False
    officer_discord_user_id: Optional[int] = None
    officer_name: str = ""


@dataclass(frozen=True)
class SiphonCacheResult:
    status: SiphonCacheStatus
    player: Optional[PlayerRecord]


@dataclass(frozen=True)
class SiphonUpdate:
    discord_user_id: int
    siphon: Optional[int]
    expected_revision: int


@dataclass(frozen=True)
class HistoryImportResult:
    status: HistoryImportStatus
    event_id: str
    player: Optional[PlayerRecord]


@dataclass(frozen=True)
class LedgerGeneration:
    """One durable, non-destructive economy ledger for a Discord server."""

    ledger_id: int
    discord_guild_id: int
    generation: int
    target_guild_name: str
    target_guild_key: str
    is_active: bool
    created_at: str
    archived_at: Optional[str]


@dataclass(frozen=True)
class OutboxEvent:
    event_id: str
    guild_id: int
    event_type: str
    aggregate_type: str
    aggregate_id: str
    payload: Mapping[str, Any]
    status: str
    attempts: int
    available_at: str
    created_at: str
    last_error: Optional[str]
    lease_owner: Optional[str]
    lease_until: Optional[str]


@dataclass(frozen=True)
class DeadLetterEvent:
    event_id: str
    guild_id: int
    event_type: str
    aggregate_type: str
    aggregate_id: str
    payload: Mapping[str, Any]
    attempts: int
    available_at: str
    created_at: str
    last_error: Optional[str]
    dead_lettered_at: str


@dataclass(frozen=True)
class OutboxStatus:
    guild_id: int
    pending_events: int
    processing_events: int
    completed_events: int
    dead_letter_events: int
    oldest_incomplete_at: Optional[str]
    last_completed_at: Optional[str]
    latest_error: Optional[str]

    @property
    def queued_events(self) -> int:
        return self.pending_events + self.processing_events

    @property
    def incomplete_events(self) -> int:
        return self.queued_events + self.dead_letter_events


@dataclass(frozen=True)
class SheetImport:
    import_id: str
    guild_id: int
    source_name: str
    source_fingerprint: str
    status: SheetImportStatus
    metadata: Mapping[str, Any]
    started_at: str
    completed_at: Optional[str]
    row_count: int
    error: Optional[str]


@dataclass(frozen=True)
class SheetImportSnapshot:
    snapshot_id: str
    guild_id: int
    source_name: str
    source_fingerprint: str
    status: SheetSnapshotStatus
    metadata: Mapping[str, Any]
    snapshot: Mapping[str, Any]
    created_at: str
    applied_at: Optional[str]


@dataclass(frozen=True)
class MigrationIssue:
    issue_id: str
    guild_id: Optional[int]
    source: str
    source_reference: str
    code: str
    message: str
    payload: Mapping[str, Any]
    created_at: str
    resolved_at: Optional[str]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS local_repository_schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS guild_ledger_generations (
    ledger_id INTEGER PRIMARY KEY CHECK (ledger_id > 0),
    discord_guild_id INTEGER NOT NULL CHECK (discord_guild_id > 0),
    generation INTEGER NOT NULL CHECK (generation > 0),
    target_guild_name TEXT NOT NULL DEFAULT '',
    target_guild_key TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('active', 'archived')),
    created_at TEXT NOT NULL,
    archived_at TEXT,
    UNIQUE (discord_guild_id, generation)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_guild_ledger_one_active
    ON guild_ledger_generations (discord_guild_id)
    WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_guild_ledger_target
    ON guild_ledger_generations (target_guild_key, status);
CREATE TABLE IF NOT EXISTS registered_players (
    guild_id INTEGER NOT NULL CHECK (guild_id > 0),
    discord_user_id INTEGER NOT NULL CHECK (discord_user_id > 0),
    nickname TEXT NOT NULL CHECK (trim(nickname) <> ''),
    nickname_key TEXT NOT NULL CHECK (trim(nickname_key) <> ''),
    albion_player_id TEXT COLLATE NOCASE,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    silver INTEGER NOT NULL DEFAULT 0 CHECK (silver >= 0),
    all_time_earnings INTEGER NOT NULL DEFAULT 0 CHECK (all_time_earnings >= 0),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    siphon INTEGER,
    siphon_revision INTEGER CHECK (
        siphon_revision IS NULL OR siphon_revision >= 1
    ),
    siphon_synced_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, discord_user_id),
    UNIQUE (guild_id, nickname_key),
    UNIQUE (guild_id, albion_player_id)
);

CREATE INDEX IF NOT EXISTS idx_registered_players_active
    ON registered_players (guild_id, is_active, nickname_key);
CREATE INDEX IF NOT EXISTS idx_registered_players_silver_rank
    ON registered_players (guild_id, silver DESC, nickname_key, discord_user_id);
CREATE INDEX IF NOT EXISTS idx_registered_players_negative_siphon
    ON registered_players (guild_id, siphon)
    WHERE siphon < 0;

CREATE TABLE IF NOT EXISTS lootsplits (
    lootsplit_id TEXT PRIMARY KEY,
    guild_id INTEGER NOT NULL CHECK (guild_id > 0),
    request_hash TEXT NOT NULL,
    battleboard_ids_json TEXT NOT NULL,
    actor_discord_user_id INTEGER,
    actor_name TEXT NOT NULL,
    officer_discord_user_id INTEGER,
    officer_name TEXT NOT NULL DEFAULT '',
    content_name TEXT NOT NULL,
    caller_discord_user_id INTEGER,
    caller_name TEXT NOT NULL,
    amount_per_participant INTEGER NOT NULL CHECK (amount_per_participant >= 0),
    missing_nicknames_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lootsplits_guild_created
    ON lootsplits (guild_id, created_at);

CREATE TABLE IF NOT EXISTS balance_history (
    event_id TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    guild_id INTEGER NOT NULL,
    discord_user_id INTEGER,
    nickname_snapshot TEXT NOT NULL,
    event_kind TEXT NOT NULL CHECK (
        event_kind IN ('manual', 'lootsplit', 'sheet_import')
    ),
    requested_delta INTEGER NOT NULL,
    actual_delta INTEGER NOT NULL,
    previous_balance INTEGER,
    updated_balance INTEGER,
    actor_discord_user_id INTEGER,
    actor_name TEXT NOT NULL,
    reason TEXT NOT NULL,
    lootsplit_id TEXT,
    occurred_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (guild_id, discord_user_id)
        REFERENCES registered_players (guild_id, discord_user_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (lootsplit_id) REFERENCES lootsplits (lootsplit_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_balance_history_player_time
    ON balance_history (guild_id, discord_user_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_balance_history_lootsplit
    ON balance_history (lootsplit_id);

CREATE TRIGGER IF NOT EXISTS balance_history_prevent_update
BEFORE UPDATE ON balance_history
BEGIN
    SELECT RAISE(ABORT, 'balance_history is immutable');
END;

CREATE TRIGGER IF NOT EXISTS balance_history_prevent_delete
BEFORE DELETE ON balance_history
BEGIN
    SELECT RAISE(ABORT, 'balance_history is immutable');
END;

CREATE TABLE IF NOT EXISTS lootsplit_participants (
    lootsplit_id TEXT NOT NULL,
    participant_index INTEGER NOT NULL CHECK (participant_index >= 0),
    guild_id INTEGER NOT NULL,
    discord_user_id INTEGER NOT NULL,
    nickname_snapshot TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK (amount >= 0),
    previous_balance INTEGER NOT NULL CHECK (previous_balance >= 0),
    updated_balance INTEGER NOT NULL CHECK (updated_balance >= 0),
    player_revision INTEGER NOT NULL CHECK (player_revision >= 1),
    history_event_id TEXT NOT NULL UNIQUE,
    PRIMARY KEY (lootsplit_id, participant_index),
    FOREIGN KEY (lootsplit_id) REFERENCES lootsplits (lootsplit_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (guild_id, discord_user_id)
        REFERENCES registered_players (guild_id, discord_user_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (history_event_id) REFERENCES balance_history (event_id)
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS imported_lootsplit_history (
    event_id TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    guild_id INTEGER NOT NULL CHECK (guild_id > 0),
    discord_user_id INTEGER,
    nickname_snapshot TEXT NOT NULL CHECK (trim(nickname_snapshot) <> ''),
    battleboard_ids_json TEXT NOT NULL,
    amount INTEGER NOT NULL,
    actor_name TEXT NOT NULL,
    content_name TEXT NOT NULL,
    caller_name TEXT NOT NULL,
    source_key TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (guild_id, source_key),
    FOREIGN KEY (guild_id, discord_user_id)
        REFERENCES registered_players (guild_id, discord_user_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_imported_lootsplit_history_guild_time
    ON imported_lootsplit_history (guild_id, occurred_at);

CREATE TRIGGER IF NOT EXISTS imported_lootsplit_history_prevent_update
BEFORE UPDATE ON imported_lootsplit_history
BEGIN
    SELECT RAISE(ABORT, 'imported_lootsplit_history is immutable');
END;

CREATE TRIGGER IF NOT EXISTS imported_lootsplit_history_prevent_delete
BEFORE DELETE ON imported_lootsplit_history
BEGIN
    SELECT RAISE(ABORT, 'imported_lootsplit_history is immutable');
END;

CREATE TABLE IF NOT EXISTS google_sync_outbox (
    event_id TEXT PRIMARY KEY,
    guild_id INTEGER NOT NULL CHECK (guild_id > 0),
    event_type TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'processing', 'completed')
    ),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at TEXT NOT NULL,
    lease_owner TEXT,
    lease_until TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_google_sync_outbox_pending
    ON google_sync_outbox (status, available_at, created_at);
CREATE INDEX IF NOT EXISTS idx_google_sync_outbox_guild_fifo
    ON google_sync_outbox (guild_id, created_at, event_id, status);
CREATE INDEX IF NOT EXISTS idx_google_sync_outbox_incomplete_fifo
    ON google_sync_outbox (guild_id, created_at, event_id)
    WHERE status != 'completed';
CREATE INDEX IF NOT EXISTS idx_google_sync_outbox_completed_at
    ON google_sync_outbox (completed_at)
    WHERE status = 'completed';
CREATE INDEX IF NOT EXISTS idx_google_sync_outbox_guild_completed_at
    ON google_sync_outbox (guild_id, completed_at, event_id)
    WHERE status = 'completed';

CREATE TABLE IF NOT EXISTS google_sync_dead_letters (
    event_id TEXT PRIMARY KEY,
    guild_id INTEGER NOT NULL CHECK (guild_id > 0),
    event_type TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    attempts INTEGER NOT NULL CHECK (attempts >= 1),
    available_at TEXT NOT NULL,
    last_error TEXT,
    created_at TEXT NOT NULL,
    dead_lettered_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_google_sync_dead_letters_guild_time
    ON google_sync_dead_letters (guild_id, dead_lettered_at, event_id);

CREATE TABLE IF NOT EXISTS sheet_imports (
    import_id TEXT PRIMARY KEY,
    guild_id INTEGER NOT NULL CHECK (guild_id > 0),
    source_name TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    metadata_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    row_count INTEGER NOT NULL DEFAULT 0 CHECK (row_count >= 0),
    error TEXT,
    UNIQUE (guild_id, source_name, source_fingerprint)
);

CREATE TABLE IF NOT EXISTS sheet_import_rows (
    import_id TEXT NOT NULL,
    worksheet_kind TEXT NOT NULL,
    row_number INTEGER NOT NULL CHECK (row_number > 0),
    row_fingerprint TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    imported_at TEXT NOT NULL,
    PRIMARY KEY (import_id, worksheet_kind, row_number),
    FOREIGN KEY (import_id) REFERENCES sheet_imports (import_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS sheet_import_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    guild_id INTEGER NOT NULL CHECK (guild_id > 0),
    source_name TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('staged', 'applied')),
    metadata_json TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    applied_at TEXT,
    UNIQUE (guild_id, source_name, source_fingerprint)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_sheet_import_one_staged_snapshot
    ON sheet_import_snapshots (guild_id, source_name)
    WHERE status = 'staged';
CREATE INDEX IF NOT EXISTS idx_sheet_import_applied_snapshot_time
    ON sheet_import_snapshots (applied_at)
    WHERE status = 'applied';
CREATE INDEX IF NOT EXISTS idx_sheet_import_guild_applied_snapshot_time
    ON sheet_import_snapshots (guild_id, applied_at, snapshot_id)
    WHERE status = 'applied';

CREATE INDEX IF NOT EXISTS idx_sheet_import_rows_fingerprint
    ON sheet_import_rows (import_id, worksheet_kind, row_fingerprint);

CREATE TABLE IF NOT EXISTS migration_issues (
    issue_id TEXT PRIMARY KEY,
    guild_id INTEGER,
    source TEXT NOT NULL,
    source_reference TEXT NOT NULL,
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_migration_issues_open
    ON migration_issues (guild_id, source, created_at)
    WHERE resolved_at IS NULL;

INSERT OR IGNORE INTO local_repository_schema_migrations (version, name)
VALUES (1, 'players economy outbox and Sheet import persistence');
"""

_SIPHON_REVISION_MIGRATION_VERSION = 2
_SIPHON_REVISION_MIGRATION_NAME = "bind cached Siphon to a player revision"
_LEDGER_GENERATION_MIGRATION_VERSION = 3
_LEDGER_GENERATION_MIGRATION_NAME = "archive business data by setup generation"
_LOOTSPLIT_OFFICER_MIGRATION_VERSION = 4
_LOOTSPLIT_OFFICER_MIGRATION_NAME = "preserve operational lootsplit officer"
_STORAGE_HARDENING_MIGRATION_VERSION = 5
_STORAGE_HARDENING_MIGRATION_NAME = "target uniqueness outbox recovery and retention"
_ALL_TIME_EARNINGS_MIGRATION_VERSION = 6
_ALL_TIME_EARNINGS_MIGRATION_NAME = "track cumulative positive local earnings"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _as_utc_string(value: Optional[str | datetime]) -> str:
    if value is None:
        return _utc_now()
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            raise ValueError("timestamp must not be blank")
        return normalized
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _validate_identifier(value: int, field_name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > SQLITE_INTEGER_MAX
    ):
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _validate_integer(value: int, field_name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < SQLITE_INTEGER_MIN
        or value > SQLITE_INTEGER_MAX
    ):
        raise ValueError(f"{field_name} must be a signed 64-bit integer")
    return value


def _validate_boolean(value: bool, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _clean_nickname(nickname: str) -> str:
    value = unicodedata.normalize("NFKC", str(nickname or "")).strip()
    if not value:
        raise ValueError("nickname must not be blank")
    return value


def normalize_nickname(nickname: str) -> str:
    """Return the persisted, Unicode-aware case-insensitive nickname key."""

    return _clean_nickname(nickname).casefold()


def _clean_optional_string(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _decode_json(value: Any, *, location: str) -> Any:
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError) as error:
        raise RepositoryCorruptionError(
            f"Persisted repository JSON is corrupt at {location}."
        ) from error


def _decode_mapping_json(value: Any, *, location: str) -> Mapping[str, Any]:
    decoded = _decode_json(value, location=location)
    if not isinstance(decoded, dict):
        raise RepositoryCorruptionError(
            f"Persisted repository JSON must be an object at {location}."
        )
    return decoded


def _decode_list_json(value: Any, *, location: str) -> list[Any]:
    decoded = _decode_json(value, location=location)
    if not isinstance(decoded, list):
        raise RepositoryCorruptionError(
            f"Persisted repository JSON must be an array at {location}."
        )
    return decoded


def _request_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _event_uuid(kind: str, idempotency_key: Optional[str] = None) -> str:
    if idempotency_key is None:
        return str(uuid4())
    key = str(idempotency_key).strip()
    if not key:
        raise ValueError("idempotency_key must not be blank")
    return str(uuid5(_EVENT_NAMESPACE, f"{kind}:{key}"))


def _derived_uuid(*parts: object) -> str:
    return str(uuid5(_EVENT_NAMESPACE, ":".join(str(part) for part in parts)))


def ensure_schema(database_path: DatabasePath = None) -> None:
    """Create this repository's tables without modifying existing data."""

    resolved_path = sqlite_database.initialize_database(database_path).resolve()
    with _SCHEMA_INITIALIZATION_LOCK:
        try:
            stat = resolved_path.stat()
            identity = (stat.st_dev, stat.st_ino)
        except OSError:
            identity = None
        if identity is not None and _SCHEMA_READY_FILES.get(resolved_path) == identity:
            return
        with sqlite_database.connection(resolved_path) as connection:
            connection.executescript(_SCHEMA)
            _migrate_siphon_revision(connection)
            _migrate_ledger_generations(connection)
            _migrate_lootsplit_officer(connection)
            _migrate_storage_hardening(connection)
            _migrate_all_time_earnings(connection)
        stat = resolved_path.stat()
        _SCHEMA_READY_FILES[resolved_path] = (stat.st_dev, stat.st_ino)


def _migrate_siphon_revision(connection: sqlite3.Connection) -> None:
    """Add Siphon provenance and distrust caches created by the old schema."""

    connection.execute("BEGIN IMMEDIATE")
    try:
        # Check the ledger only after taking the write lock. A standalone
        # migration script and the bot may initialize the same database at once.
        applied = connection.execute(
            "SELECT 1 FROM local_repository_schema_migrations WHERE version = ?",
            (_SIPHON_REVISION_MIGRATION_VERSION,),
        ).fetchone()
        if applied is not None:
            connection.execute("COMMIT")
            return

        columns = {
            str(row["name"]) for row in connection.execute("PRAGMA table_info(registered_players)")
        }
        if "siphon_revision" not in columns:
            connection.execute(
                """
                ALTER TABLE registered_players
                ADD COLUMN siphon_revision INTEGER CHECK (
                    siphon_revision IS NULL OR siphon_revision >= 1
                )
                """
            )
        # Old cached values cannot prove which local revision their Sheet formula
        # used. Clearing them is safer than blessing a potentially stale balance.
        connection.execute(
            """
            UPDATE registered_players
            SET siphon = NULL, siphon_revision = NULL, siphon_synced_at = NULL
            WHERE siphon IS NOT NULL
                OR siphon_revision IS NOT NULL
                OR siphon_synced_at IS NOT NULL
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_registered_players_current_negative_siphon
            ON registered_players (guild_id, siphon_revision, revision, siphon)
            WHERE siphon < 0
            """
        )
        connection.execute(
            """
            INSERT INTO local_repository_schema_migrations (version, name)
            VALUES (?, ?)
            """,
            (_SIPHON_REVISION_MIGRATION_VERSION, _SIPHON_REVISION_MIGRATION_NAME),
        )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def _normalize_target_guild(value: str) -> tuple[str, str]:
    display_name = unicodedata.normalize("NFKC", str(value or "")).strip()
    return display_name, display_name.casefold()


def _raise_target_conflict(
    connection: sqlite3.Connection,
    *,
    discord_guild_id: int,
    target_guild_key: str,
) -> None:
    owner = connection.execute(
        """
        SELECT discord_guild_id FROM guild_ledger_generations
        WHERE target_guild_key = ? AND status = 'active'
            AND discord_guild_id != ?
        LIMIT 1
        """,
        (target_guild_key, discord_guild_id),
    ).fetchone()
    if owner is not None:
        raise TargetGuildConflictError(
            "This Albion guild already belongs to the active ledger for Discord "
            f"server {int(owner['discord_guild_id'])}."
        )


def _require_active_ledger(
    connection: sqlite3.Connection,
    ledger_id: int,
) -> LedgerGeneration:
    """Validate ledger authority under the caller's transaction lock."""

    row = connection.execute(
        """
        SELECT * FROM guild_ledger_generations
        WHERE ledger_id = ? AND status = 'active' AND target_guild_key <> ''
        """,
        (ledger_id,),
    ).fetchone()
    if row is None:
        raise LedgerNotActiveError(
            f"ledger {ledger_id} is missing, archived, or has no configured Albion guild"
        )
    return _row_to_ledger_generation(row)


def _row_to_ledger_generation(row: sqlite3.Row) -> LedgerGeneration:
    return LedgerGeneration(
        ledger_id=int(row["ledger_id"]),
        discord_guild_id=int(row["discord_guild_id"]),
        generation=int(row["generation"]),
        target_guild_name=str(row["target_guild_name"]),
        target_guild_key=str(row["target_guild_key"]),
        is_active=str(row["status"]) == "active",
        created_at=str(row["created_at"]),
        archived_at=row["archived_at"],
    )


def _migrate_ledger_generations(connection: sqlite3.Connection) -> None:
    """Bind pre-generation rows to configured guilds and archive orphaned data.

    A historical ledger with no live ``guild_settings`` document most likely
    belongs to a removed setup.  It is retained as generation one but is not
    made active, preventing a later setup from inheriting its balances.
    """

    connection.execute("BEGIN IMMEDIATE")
    try:
        applied = connection.execute(
            "SELECT 1 FROM local_repository_schema_migrations WHERE version = ?",
            (_LEDGER_GENERATION_MIGRATION_VERSION,),
        ).fetchone()
        if applied is not None:
            connection.execute("COMMIT")
            return

        guild_rows = connection.execute(
            """
            SELECT guild_id FROM registered_players
            UNION SELECT guild_id FROM lootsplits
            UNION SELECT guild_id FROM balance_history
            UNION SELECT guild_id FROM imported_lootsplit_history
            UNION SELECT guild_id FROM google_sync_outbox
            UNION SELECT guild_id FROM sheet_imports
            UNION SELECT guild_id FROM migration_issues WHERE guild_id IS NOT NULL
            """
        ).fetchall()
        now = _utc_now()
        for guild_row in guild_rows:
            guild_id = int(guild_row["guild_id"])
            if (
                connection.execute(
                    "SELECT 1 FROM guild_ledger_generations WHERE ledger_id = ?",
                    (guild_id,),
                ).fetchone()
                is not None
            ):
                continue
            config_row = connection.execute(
                """
                SELECT payload_json FROM configuration_documents
                WHERE namespace = 'guild_settings' AND guild_id = ?
                """,
                (str(guild_id),),
            ).fetchone()
            target_name = ""
            if config_row is not None:
                try:
                    payload = json.loads(config_row["payload_json"])
                    if isinstance(payload, str):
                        target_name = payload
                    elif isinstance(payload, Mapping):
                        target_name = str(payload.get("guild_name") or "")
                except (json.JSONDecodeError, TypeError):
                    target_name = ""
            target_name, target_key = _normalize_target_guild(target_name)
            is_active = bool(target_key)
            connection.execute(
                """
                INSERT INTO guild_ledger_generations (
                    ledger_id, discord_guild_id, generation,
                    target_guild_name, target_guild_key, status,
                    created_at, archived_at
                ) VALUES (?, ?, 1, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    guild_id,
                    target_name,
                    target_key,
                    "active" if is_active else "archived",
                    now,
                    None if is_active else now,
                ),
            )
        connection.execute(
            """
            INSERT INTO local_repository_schema_migrations (version, name)
            VALUES (?, ?)
            """,
            (_LEDGER_GENERATION_MIGRATION_VERSION, _LEDGER_GENERATION_MIGRATION_NAME),
        )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def _migrate_lootsplit_officer(connection: sqlite3.Connection) -> None:
    """Add nullable operational-officer attribution without rewriting history."""

    connection.execute("BEGIN IMMEDIATE")
    try:
        applied = connection.execute(
            "SELECT 1 FROM local_repository_schema_migrations WHERE version = ?",
            (_LOOTSPLIT_OFFICER_MIGRATION_VERSION,),
        ).fetchone()
        if applied is not None:
            connection.execute("COMMIT")
            return
        columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(lootsplits)")}
        if "officer_discord_user_id" not in columns:
            connection.execute("ALTER TABLE lootsplits ADD COLUMN officer_discord_user_id INTEGER")
        if "officer_name" not in columns:
            connection.execute(
                "ALTER TABLE lootsplits ADD COLUMN officer_name TEXT NOT NULL DEFAULT ''"
            )
        connection.execute(
            "INSERT INTO local_repository_schema_migrations (version, name) VALUES (?, ?)",
            (_LOOTSPLIT_OFFICER_MIGRATION_VERSION, _LOOTSPLIT_OFFICER_MIGRATION_NAME),
        )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def _migrate_storage_hardening(connection: sqlite3.Connection) -> None:
    """Install target ownership, retry recovery, and retention indexes."""

    connection.execute("BEGIN IMMEDIATE")
    try:
        applied = connection.execute(
            "SELECT 1 FROM local_repository_schema_migrations WHERE version = ?",
            (_STORAGE_HARDENING_MIGRATION_VERSION,),
        ).fetchone()
        if applied is not None:
            connection.execute("COMMIT")
            return
        duplicate = connection.execute(
            """
            SELECT target_guild_key, COUNT(*) AS owners
            FROM guild_ledger_generations
            WHERE status = 'active' AND target_guild_key <> ''
            GROUP BY target_guild_key HAVING COUNT(*) > 1
            LIMIT 1
            """
        ).fetchone()
        if duplicate is not None:
            raise TargetGuildConflictError(
                "multiple active ledgers already claim the normalized Albion guild "
                f"{duplicate['target_guild_key']!r}; resolve the ownership before startup"
            )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_guild_ledger_one_active_target
            ON guild_ledger_generations (target_guild_key)
            WHERE status = 'active' AND target_guild_key <> ''
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_google_sync_outbox_incomplete_fifo
            ON google_sync_outbox (guild_id, created_at, event_id)
            WHERE status != 'completed'
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_google_sync_outbox_completed_at
            ON google_sync_outbox (completed_at)
            WHERE status = 'completed'
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS google_sync_dead_letters (
                event_id TEXT PRIMARY KEY,
                guild_id INTEGER NOT NULL CHECK (guild_id > 0),
                event_type TEXT NOT NULL,
                aggregate_type TEXT NOT NULL,
                aggregate_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                attempts INTEGER NOT NULL CHECK (attempts >= 1),
                available_at TEXT NOT NULL,
                last_error TEXT,
                created_at TEXT NOT NULL,
                dead_lettered_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_google_sync_dead_letters_guild_time
            ON google_sync_dead_letters (guild_id, dead_lettered_at, event_id)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_sheet_import_applied_snapshot_time
            ON sheet_import_snapshots (applied_at)
            WHERE status = 'applied'
            """
        )
        connection.execute(
            "INSERT INTO local_repository_schema_migrations (version, name) VALUES (?, ?)",
            (_STORAGE_HARDENING_MIGRATION_VERSION, _STORAGE_HARDENING_MIGRATION_NAME),
        )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def _migrate_all_time_earnings(connection: sqlite3.Connection) -> None:
    """Add cumulative earnings and reconstruct them from immutable local history."""

    connection.execute("BEGIN IMMEDIATE")
    try:
        applied = connection.execute(
            "SELECT 1 FROM local_repository_schema_migrations WHERE version = ?",
            (_ALL_TIME_EARNINGS_MIGRATION_VERSION,),
        ).fetchone()
        if applied is not None:
            connection.execute("COMMIT")
            return

        columns = {
            str(row["name"]) for row in connection.execute("PRAGMA table_info(registered_players)")
        }
        if "all_time_earnings" not in columns:
            connection.execute(
                """
                ALTER TABLE registered_players
                ADD COLUMN all_time_earnings INTEGER NOT NULL DEFAULT 0
                    CHECK (all_time_earnings >= 0)
                """
            )
        connection.execute(
            """
            UPDATE registered_players
            SET all_time_earnings = COALESCE((
                SELECT SUM(history.actual_delta)
                FROM balance_history AS history
                WHERE history.guild_id = registered_players.guild_id
                    AND history.discord_user_id = registered_players.discord_user_id
                    AND history.event_kind IN ('manual', 'lootsplit', 'sheet_import')
                    AND history.actual_delta > 0
            ), 0)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_registered_players_silver_rank
            ON registered_players (guild_id, silver DESC, nickname_key, discord_user_id)
            """
        )
        connection.execute(
            "INSERT INTO local_repository_schema_migrations (version, name) VALUES (?, ?)",
            (_ALL_TIME_EARNINGS_MIGRATION_VERSION, _ALL_TIME_EARNINGS_MIGRATION_NAME),
        )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def _ledger_storage_id_is_available(
    connection: sqlite3.Connection,
    ledger_id: int,
) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM guild_ledger_generations WHERE ledger_id = ?",
            (ledger_id,),
        ).fetchone()
        is None
    )


def _allocate_ledger_id(
    connection: sqlite3.Connection,
    discord_guild_id: int,
    generation: int,
) -> int:
    if generation == 1 and _ledger_storage_id_is_available(
        connection,
        discord_guild_id,
    ):
        return discord_guild_id
    for nonce in range(10_000):
        digest = hashlib.sha256(
            f"realm-ledger:{discord_guild_id}:{generation}:{nonce}".encode("ascii")
        ).digest()
        # Keep generated IDs inside SQLite's signed range and visually separate
        # them from today's Discord snowflakes.
        candidate = (int.from_bytes(digest[:8], "big") & ((1 << 62) - 1)) | (1 << 62)
        if _ledger_storage_id_is_available(connection, candidate):
            return candidate
    raise RepositoryError("could not allocate a unique ledger generation ID")


def activate_ledger_in_transaction(
    connection: sqlite3.Connection,
    discord_guild_id: int,
    target_guild_name: str,
) -> LedgerGeneration:
    """Return the matching active ledger, rotating on target-guild changes.

    The caller owns the SQLite transaction. This lets configuration and ledger
    identity change atomically in ``guild_settings``.
    """

    _validate_identifier(discord_guild_id, "discord_guild_id")
    target_name, target_key = _normalize_target_guild(target_guild_name)
    if not target_key:
        raise ValueError("target_guild_name must not be blank")
    _raise_target_conflict(
        connection,
        discord_guild_id=discord_guild_id,
        target_guild_key=target_key,
    )
    active = connection.execute(
        """
        SELECT * FROM guild_ledger_generations
        WHERE discord_guild_id = ? AND status = 'active'
        """,
        (discord_guild_id,),
    ).fetchone()
    if active is not None:
        active_key = str(active["target_guild_key"])
        if not active_key or active_key == target_key:
            if str(active["target_guild_name"]) != target_name or active_key != target_key:
                try:
                    connection.execute(
                        """
                        UPDATE guild_ledger_generations
                        SET target_guild_name = ?, target_guild_key = ?
                        WHERE ledger_id = ?
                        """,
                        (target_name, target_key, int(active["ledger_id"])),
                    )
                except sqlite3.IntegrityError as error:
                    raise TargetGuildConflictError(
                        "This Albion guild already belongs to another active ledger."
                    ) from error
                active = connection.execute(
                    "SELECT * FROM guild_ledger_generations WHERE ledger_id = ?",
                    (int(active["ledger_id"]),),
                ).fetchone()
            return _row_to_ledger_generation(active)

        archived_at = _utc_now()
        connection.execute(
            """
            UPDATE guild_ledger_generations
            SET status = 'archived', archived_at = ?
            WHERE ledger_id = ? AND status = 'active'
            """,
            (archived_at, int(active["ledger_id"])),
        )

    generation = int(
        connection.execute(
            """
            SELECT COALESCE(MAX(generation), 0) + 1
            FROM guild_ledger_generations WHERE discord_guild_id = ?
            """,
            (discord_guild_id,),
        ).fetchone()[0]
    )
    ledger_id = _allocate_ledger_id(connection, discord_guild_id, generation)
    now = _utc_now()
    try:
        connection.execute(
            """
            INSERT INTO guild_ledger_generations (
                ledger_id, discord_guild_id, generation,
                target_guild_name, target_guild_key, status, created_at
            ) VALUES (?, ?, ?, ?, ?, 'active', ?)
            """,
            (
                ledger_id,
                discord_guild_id,
                generation,
                target_name,
                target_key,
                now,
            ),
        )
    except sqlite3.IntegrityError as error:
        raise TargetGuildConflictError(
            "This Albion guild already belongs to another active ledger."
        ) from error
    row = connection.execute(
        "SELECT * FROM guild_ledger_generations WHERE ledger_id = ?",
        (ledger_id,),
    ).fetchone()
    return _row_to_ledger_generation(row)


def archive_active_ledger_in_transaction(
    connection: sqlite3.Connection,
    discord_guild_id: int,
) -> Optional[LedgerGeneration]:
    _validate_identifier(discord_guild_id, "discord_guild_id")
    row = connection.execute(
        """
        SELECT * FROM guild_ledger_generations
        WHERE discord_guild_id = ? AND status = 'active'
        """,
        (discord_guild_id,),
    ).fetchone()
    if row is None:
        return None
    archived_at = _utc_now()
    connection.execute(
        """
        UPDATE guild_ledger_generations
        SET status = 'archived', archived_at = ?
        WHERE ledger_id = ? AND status = 'active'
        """,
        (archived_at, int(row["ledger_id"])),
    )
    archived = connection.execute(
        "SELECT * FROM guild_ledger_generations WHERE ledger_id = ?",
        (int(row["ledger_id"]),),
    ).fetchone()
    return _row_to_ledger_generation(archived)


def activate_ledger(
    discord_guild_id: int,
    target_guild_name: str,
    *,
    database_path: DatabasePath = None,
) -> LedgerGeneration:
    with _transaction(database_path) as connection:
        return activate_ledger_in_transaction(
            connection,
            discord_guild_id,
            target_guild_name,
        )


def get_active_ledger(
    discord_guild_id: int,
    *,
    create_if_missing: bool = False,
    target_guild_name: Optional[str] = None,
    database_path: DatabasePath = None,
) -> Optional[LedgerGeneration]:
    _validate_identifier(discord_guild_id, "discord_guild_id")
    if create_if_missing:
        target_name, target_key = _normalize_target_guild(target_guild_name or "")
        if not target_key:
            raise ValueError("target_guild_name is required when create_if_missing is true")
        with _transaction(database_path) as connection:
            row = connection.execute(
                """
                SELECT * FROM guild_ledger_generations
                WHERE discord_guild_id = ? AND status = 'active'
                """,
                (discord_guild_id,),
            ).fetchone()
            if row is not None:
                if str(row["target_guild_key"]) != target_key:
                    raise RepositoryError(
                        "an active ledger exists for a different Albion guild; "
                        "use activate_ledger to rotate it explicitly"
                    )
                return _row_to_ledger_generation(row)
            return activate_ledger_in_transaction(
                connection,
                discord_guild_id,
                target_name,
            )
    with _connection(database_path) as connection:
        row = connection.execute(
            """
            SELECT * FROM guild_ledger_generations
            WHERE discord_guild_id = ? AND status = 'active'
            """,
            (discord_guild_id,),
        ).fetchone()
        return None if row is None else _row_to_ledger_generation(row)


def get_active_ledger_id(
    discord_guild_id: int,
    *,
    create_if_missing: bool = False,
    target_guild_name: Optional[str] = None,
    database_path: DatabasePath = None,
) -> Optional[int]:
    ledger = get_active_ledger(
        discord_guild_id,
        create_if_missing=create_if_missing,
        target_guild_name=target_guild_name,
        database_path=database_path,
    )
    return None if ledger is None else ledger.ledger_id


def list_ledger_generations(
    discord_guild_id: int,
    *,
    database_path: DatabasePath = None,
) -> list[LedgerGeneration]:
    _validate_identifier(discord_guild_id, "discord_guild_id")
    with _connection(database_path) as connection:
        return [
            _row_to_ledger_generation(row)
            for row in connection.execute(
                """
                SELECT * FROM guild_ledger_generations
                WHERE discord_guild_id = ? ORDER BY generation
                """,
                (discord_guild_id,),
            ).fetchall()
        ]


@contextmanager
def _connection(database_path: DatabasePath) -> Iterator[sqlite3.Connection]:
    ensure_schema(database_path)
    with sqlite_database.connection(database_path) as connection:
        yield connection


@contextmanager
def _transaction(database_path: DatabasePath) -> Iterator[sqlite3.Connection]:
    ensure_schema(database_path)
    with sqlite_database.transaction(database_path) as connection:
        yield connection


def _row_to_player(row: sqlite3.Row) -> PlayerRecord:
    return PlayerRecord(
        guild_id=int(row["guild_id"]),
        discord_user_id=int(row["discord_user_id"]),
        nickname=str(row["nickname"]),
        nickname_key=str(row["nickname_key"]),
        albion_player_id=row["albion_player_id"],
        is_active=bool(row["is_active"]),
        silver=int(row["silver"]),
        all_time_earnings=int(row["all_time_earnings"]),
        revision=int(row["revision"]),
        siphon=None if row["siphon"] is None else int(row["siphon"]),
        siphon_revision=(None if row["siphon_revision"] is None else int(row["siphon_revision"])),
        siphon_synced_at=row["siphon_synced_at"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _get_player_row(
    connection: sqlite3.Connection,
    guild_id: int,
    discord_user_id: int,
) -> Optional[sqlite3.Row]:
    return connection.execute(
        """
        SELECT * FROM registered_players
        WHERE guild_id = ? AND discord_user_id = ?
        """,
        (guild_id, discord_user_id),
    ).fetchone()


def _require_player_row(
    connection: sqlite3.Connection,
    guild_id: int,
    discord_user_id: int,
) -> sqlite3.Row:
    """Read back a player after a successful write or flag database corruption."""

    row = _get_player_row(connection, guild_id, discord_user_id)
    if row is None:
        raise RepositoryCorruptionError(
            "A player row disappeared during an atomic repository mutation."
        )
    return row


def _player_payload(player: PlayerRecord) -> dict[str, Any]:
    return {
        "guild_id": player.guild_id,
        "discord_user_id": player.discord_user_id,
        "albion_player_id": player.albion_player_id,
        "nickname": player.nickname,
        "is_in_guild": player.is_active,
        "silver": player.silver,
        "revision": player.revision,
    }


def _enqueue_outbox(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    guild_id: int,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: Mapping[str, Any],
    now: str,
) -> None:
    payload_json = _canonical_json(payload)
    existing = connection.execute(
        "SELECT payload_json, event_type FROM google_sync_outbox WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    if existing is not None:
        if existing["payload_json"] != payload_json or existing["event_type"] != event_type:
            raise IdempotencyConflictError(
                f"outbox event {event_id} already exists for a different payload"
            )
        return

    connection.execute(
        """
        INSERT INTO google_sync_outbox (
            event_id, guild_id, event_type, aggregate_type, aggregate_id,
            payload_json, status, attempts, available_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
        """,
        (
            event_id,
            guild_id,
            event_type,
            aggregate_type,
            aggregate_id,
            payload_json,
            now,
            now,
        ),
    )


def register_player(
    guild_id: int,
    discord_user_id: int,
    nickname: str,
    albion_player_id: Optional[str] = None,
    *,
    database_path: DatabasePath = None,
) -> RegistrationResult:
    """Create or reactivate a registration without ever resetting Silver."""

    _validate_identifier(guild_id, "guild_id")
    _validate_identifier(discord_user_id, "discord_user_id")
    clean_nickname = _clean_nickname(nickname)
    nickname_key = normalize_nickname(clean_nickname)
    clean_albion_id = _clean_optional_string(albion_player_id)
    now = _utc_now()

    with _transaction(database_path) as connection:
        _require_active_ledger(connection, guild_id)
        nickname_owner = connection.execute(
            """
            SELECT * FROM registered_players
            WHERE guild_id = ? AND nickname_key = ?
            """,
            (guild_id, nickname_key),
        ).fetchone()
        if nickname_owner is not None and int(nickname_owner["discord_user_id"]) != discord_user_id:
            return RegistrationResult(
                RegistrationStatus.NICKNAME_CONFLICT,
                None,
                int(nickname_owner["discord_user_id"]),
            )

        if clean_albion_id is not None:
            albion_owner = connection.execute(
                """
                SELECT * FROM registered_players
                WHERE guild_id = ? AND albion_player_id = ?
                """,
                (guild_id, clean_albion_id),
            ).fetchone()
            if albion_owner is not None and int(albion_owner["discord_user_id"]) != discord_user_id:
                return RegistrationResult(
                    RegistrationStatus.ALBION_ID_CONFLICT,
                    None,
                    int(albion_owner["discord_user_id"]),
                )

        existing = _get_player_row(connection, guild_id, discord_user_id)
        if existing is None:
            connection.execute(
                """
                INSERT INTO registered_players (
                    guild_id, discord_user_id, nickname, nickname_key,
                    albion_player_id, is_active, silver, revision,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, 0, 1, ?, ?)
                """,
                (
                    guild_id,
                    discord_user_id,
                    clean_nickname,
                    nickname_key,
                    clean_albion_id,
                    now,
                    now,
                ),
            )
            player = _row_to_player(_require_player_row(connection, guild_id, discord_user_id))
            status = RegistrationStatus.CREATED
        elif bool(existing["is_active"]):
            if existing["albion_player_id"] is None and clean_albion_id is not None:
                connection.execute(
                    """
                    UPDATE registered_players
                    SET albion_player_id = ?, updated_at = ?
                    WHERE guild_id = ? AND discord_user_id = ?
                    """,
                    (clean_albion_id, now, guild_id, discord_user_id),
                )
            player = _row_to_player(_require_player_row(connection, guild_id, discord_user_id))
            return RegistrationResult(RegistrationStatus.ALREADY_REGISTERED, player)
        else:
            next_albion_id = clean_albion_id or existing["albion_player_id"]
            connection.execute(
                """
                UPDATE registered_players
                SET nickname = ?, nickname_key = ?, albion_player_id = ?,
                    is_active = 1, revision = revision + 1,
                    siphon = NULL, siphon_revision = NULL,
                    siphon_synced_at = NULL, updated_at = ?
                WHERE guild_id = ? AND discord_user_id = ?
                """,
                (
                    clean_nickname,
                    nickname_key,
                    next_albion_id,
                    now,
                    guild_id,
                    discord_user_id,
                ),
            )
            player = _row_to_player(_require_player_row(connection, guild_id, discord_user_id))
            status = RegistrationStatus.REACTIVATED

        outbox_event_id = _derived_uuid(
            "player.upsert",
            player.guild_id,
            player.discord_user_id,
            player.revision,
        )
        _enqueue_outbox(
            connection,
            event_id=outbox_event_id,
            guild_id=guild_id,
            event_type="player.upsert",
            aggregate_type="player",
            aggregate_id=f"{guild_id}:{discord_user_id}",
            payload={"event_id": outbox_event_id, "player": _player_payload(player)},
            now=now,
        )
        return RegistrationResult(status, player)


def import_player(
    guild_id: int,
    discord_user_id: int,
    nickname: str,
    *,
    is_active: bool,
    silver: int,
    albion_player_id: Optional[str] = None,
    siphon: Optional[int] = None,
    siphon_synced_at: Optional[str | datetime] = None,
    database_path: DatabasePath = None,
) -> PlayerImportResult:
    """Bootstrap a Sheet player while preserving any existing local authority.

    When a local registration already exists, no player field is accepted from
    the bootstrap snapshot. Nickname, membership, Silver, and cached Siphon all
    remain authoritative locally until an explicit Siphon synchronization.
    """

    _validate_identifier(guild_id, "guild_id")
    _validate_identifier(discord_user_id, "discord_user_id")
    _validate_boolean(is_active, "is_active")
    clean_nickname = _clean_nickname(nickname)
    nickname_key = normalize_nickname(clean_nickname)
    clean_albion_id = _clean_optional_string(albion_player_id)
    silver = _validate_integer(silver, "silver")
    if silver < 0:
        raise ValueError("silver must not be negative")
    if siphon is not None:
        siphon = _validate_integer(siphon, "siphon")
    now = _utc_now()
    synced_at = _as_utc_string(siphon_synced_at) if siphon is not None else None

    with _transaction(database_path) as connection:
        _require_active_ledger(connection, guild_id)
        existing = _get_player_row(connection, guild_id, discord_user_id)
        if existing is not None:
            return PlayerImportResult(
                PlayerImportStatus.LOCAL_PRESERVED,
                _row_to_player(existing),
            )

        nickname_owner = connection.execute(
            """
            SELECT discord_user_id FROM registered_players
            WHERE guild_id = ? AND nickname_key = ?
            """,
            (guild_id, nickname_key),
        ).fetchone()
        if nickname_owner is not None:
            return PlayerImportResult(
                PlayerImportStatus.NICKNAME_CONFLICT,
                None,
                int(nickname_owner["discord_user_id"]),
            )

        if clean_albion_id is not None:
            albion_owner = connection.execute(
                """
                SELECT discord_user_id FROM registered_players
                WHERE guild_id = ? AND albion_player_id = ?
                """,
                (guild_id, clean_albion_id),
            ).fetchone()
            if albion_owner is not None:
                return PlayerImportResult(
                    PlayerImportStatus.ALBION_ID_CONFLICT,
                    None,
                    int(albion_owner["discord_user_id"]),
                )

        connection.execute(
            """
            INSERT INTO registered_players (
                guild_id, discord_user_id, nickname, nickname_key,
                albion_player_id, is_active, silver, revision, siphon,
                siphon_revision, siphon_synced_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                discord_user_id,
                clean_nickname,
                nickname_key,
                clean_albion_id,
                int(bool(is_active)),
                silver,
                siphon,
                1 if siphon is not None else None,
                synced_at,
                now,
                now,
            ),
        )
        player = _row_to_player(_require_player_row(connection, guild_id, discord_user_id))
        return PlayerImportResult(PlayerImportStatus.IMPORTED, player)


def get_player(
    guild_id: int,
    discord_user_id: int,
    *,
    database_path: DatabasePath = None,
) -> Optional[PlayerRecord]:
    _validate_identifier(guild_id, "guild_id")
    _validate_identifier(discord_user_id, "discord_user_id")
    with _connection(database_path) as connection:
        row = _get_player_row(connection, guild_id, discord_user_id)
        return None if row is None else _row_to_player(row)


def get_player_by_nickname(
    guild_id: int,
    nickname: str,
    *,
    database_path: DatabasePath = None,
) -> Optional[PlayerRecord]:
    _validate_identifier(guild_id, "guild_id")
    nickname_key = normalize_nickname(nickname)
    with _connection(database_path) as connection:
        row = connection.execute(
            """
            SELECT * FROM registered_players
            WHERE guild_id = ? AND nickname_key = ?
            """,
            (guild_id, nickname_key),
        ).fetchone()
        return None if row is None else _row_to_player(row)


def list_active_players(
    guild_id: int,
    *,
    database_path: DatabasePath = None,
) -> list[PlayerRecord]:
    return list_players(
        guild_id,
        active_only=True,
        database_path=database_path,
    )


def list_players(
    guild_id: int,
    *,
    active_only: Optional[bool] = None,
    database_path: DatabasePath = None,
) -> list[PlayerRecord]:
    """List local registrations, optionally filtered by membership state."""

    _validate_identifier(guild_id, "guild_id")
    if active_only is not None:
        _validate_boolean(active_only, "active_only")
    active_clause = "AND is_active = ?" if active_only is not None else ""
    parameters: list[Any] = [guild_id]
    if active_only is not None:
        parameters.append(int(active_only))
    with _connection(database_path) as connection:
        rows = connection.execute(
            f"""
            SELECT * FROM registered_players
            WHERE guild_id = ? {active_clause}
            ORDER BY nickname_key, discord_user_id
            """,
            parameters,
        ).fetchall()
        return [_row_to_player(row) for row in rows]


def get_silver_leaderboard(
    guild_id: int,
    *,
    limit: int = 10,
    offset: int = 0,
    database_path: DatabasePath = None,
) -> SilverLeaderboardPage:
    """Return one deterministic page of all registered players by Silver balance."""

    _validate_identifier(guild_id, "guild_id")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0 or limit > 100:
        raise ValueError("limit must be an integer between 1 and 100")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    with _connection(database_path) as connection:
        total_players = int(
            connection.execute(
                "SELECT COUNT(*) FROM registered_players WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()[0]
        )
        rows = connection.execute(
            """
            SELECT * FROM registered_players
            WHERE guild_id = ?
            ORDER BY silver DESC, nickname_key, discord_user_id
            LIMIT ? OFFSET ?
            """,
            (guild_id, limit, offset),
        ).fetchall()
    return SilverLeaderboardPage(
        players=tuple(_row_to_player(row) for row in rows),
        total_players=total_players,
        limit=limit,
        offset=offset,
    )


def get_silver_leaderboard_position(
    guild_id: int,
    discord_user_id: int,
    *,
    database_path: DatabasePath = None,
) -> Optional[int]:
    """Return a player's one-based position in the deterministic Silver ranking."""

    _validate_identifier(guild_id, "guild_id")
    _validate_identifier(discord_user_id, "discord_user_id")
    with _connection(database_path) as connection:
        row = connection.execute(
            """
            WITH ranked_players AS (
                SELECT
                    discord_user_id,
                    ROW_NUMBER() OVER (
                        ORDER BY silver DESC, nickname_key, discord_user_id
                    ) AS leaderboard_position
                FROM registered_players
                WHERE guild_id = ?
            )
            SELECT leaderboard_position
            FROM ranked_players
            WHERE discord_user_id = ?
            """,
            (guild_id, discord_user_id),
        ).fetchone()
    return int(row["leaderboard_position"]) if row is not None else None


def set_in_guild(
    guild_id: int,
    discord_user_id: int,
    is_in_guild: bool,
    *,
    database_path: DatabasePath = None,
) -> Optional[PlayerRecord]:
    _validate_identifier(guild_id, "guild_id")
    _validate_identifier(discord_user_id, "discord_user_id")
    _validate_boolean(is_in_guild, "is_in_guild")
    now = _utc_now()
    active_value = int(bool(is_in_guild))
    with _transaction(database_path) as connection:
        _require_active_ledger(connection, guild_id)
        existing = _get_player_row(connection, guild_id, discord_user_id)
        if existing is None:
            return None
        if int(existing["is_active"]) == active_value:
            return _row_to_player(existing)

        connection.execute(
            """
            UPDATE registered_players
            SET is_active = ?, revision = revision + 1,
                siphon = NULL, siphon_revision = NULL,
                siphon_synced_at = NULL, updated_at = ?
            WHERE guild_id = ? AND discord_user_id = ?
            """,
            (active_value, now, guild_id, discord_user_id),
        )
        player = _row_to_player(_require_player_row(connection, guild_id, discord_user_id))
        event_id = _derived_uuid(
            "player.upsert",
            guild_id,
            discord_user_id,
            player.revision,
        )
        _enqueue_outbox(
            connection,
            event_id=event_id,
            guild_id=guild_id,
            event_type="player.upsert",
            aggregate_type="player",
            aggregate_id=f"{guild_id}:{discord_user_id}",
            payload={"event_id": event_id, "player": _player_payload(player)},
            now=now,
        )
        return player


def _balance_result_from_history(
    connection: sqlite3.Connection,
    history: sqlite3.Row,
    *,
    replay: bool,
) -> BalanceChangeResult:
    player_row = _get_player_row(
        connection,
        int(history["guild_id"]),
        int(history["discord_user_id"]),
    )
    if player_row is None:
        raise RepositoryError("balance history references a missing player")
    return BalanceChangeResult(
        event_id=str(history["event_id"]),
        player=_row_to_player(player_row),
        previous_balance=int(history["previous_balance"]),
        requested_delta=int(history["requested_delta"]),
        actual_delta=int(history["actual_delta"]),
        updated_balance=int(history["updated_balance"]),
        idempotent_replay=replay,
    )


def change_balance(
    guild_id: int,
    discord_user_id: int,
    delta: int,
    *,
    actor_discord_user_id: Optional[int] = None,
    actor_name: str = "",
    reason: str = "",
    idempotency_key: Optional[str] = None,
    occurred_at: Optional[str | datetime] = None,
    database_path: DatabasePath = None,
) -> Optional[BalanceChangeResult]:
    """Atomically change Silver, clamp it at zero, and append audit/outbox rows."""

    _validate_identifier(guild_id, "guild_id")
    _validate_identifier(discord_user_id, "discord_user_id")
    delta = _validate_integer(delta, "delta")
    if actor_discord_user_id is not None:
        _validate_identifier(actor_discord_user_id, "actor_discord_user_id")
    event_id = _event_uuid(f"balance:{guild_id}", idempotency_key)
    occurred_at_value = _as_utc_string(occurred_at)
    now = _utc_now()
    request = {
        "guild_id": guild_id,
        "discord_user_id": discord_user_id,
        "delta": delta,
        "actor_discord_user_id": actor_discord_user_id,
        "actor_name": str(actor_name or ""),
        "reason": str(reason or ""),
        # An omitted timestamp is generated once for storage.  It is excluded
        # from the request identity so a retry with the same key remains safe.
        "occurred_at": occurred_at_value if occurred_at is not None else None,
    }
    request_hash = _request_hash(request)

    with _transaction(database_path) as connection:
        _require_active_ledger(connection, guild_id)
        existing_history = connection.execute(
            "SELECT * FROM balance_history WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if existing_history is not None:
            if existing_history["request_hash"] != request_hash:
                raise IdempotencyConflictError(
                    "balance idempotency key was reused for a different request"
                )
            return _balance_result_from_history(
                connection,
                existing_history,
                replay=True,
            )

        player_row = _get_player_row(connection, guild_id, discord_user_id)
        if player_row is None:
            return None
        previous = int(player_row["silver"])
        updated = max(0, previous + delta)
        _validate_integer(updated, "updated_balance")
        actual_delta = updated - previous
        all_time_earnings = int(player_row["all_time_earnings"]) + max(actual_delta, 0)
        _validate_integer(all_time_earnings, "all_time_earnings")
        revision_increment = 1 if actual_delta != 0 else 0
        connection.execute(
            """
            UPDATE registered_players
            SET silver = ?, all_time_earnings = ?, revision = revision + ?,
                siphon = CASE WHEN ? = 1 THEN NULL ELSE siphon END,
                siphon_revision = CASE
                    WHEN ? = 1 THEN NULL ELSE siphon_revision
                END,
                siphon_synced_at = CASE
                    WHEN ? = 1 THEN NULL ELSE siphon_synced_at
                END,
                updated_at = ?
            WHERE guild_id = ? AND discord_user_id = ?
            """,
            (
                updated,
                all_time_earnings,
                revision_increment,
                revision_increment,
                revision_increment,
                revision_increment,
                now,
                guild_id,
                discord_user_id,
            ),
        )
        player = _row_to_player(_require_player_row(connection, guild_id, discord_user_id))
        connection.execute(
            """
            INSERT INTO balance_history (
                event_id, request_hash, guild_id, discord_user_id,
                nickname_snapshot, event_kind, requested_delta, actual_delta,
                previous_balance, updated_balance, actor_discord_user_id,
                actor_name, reason, occurred_at, created_at
            ) VALUES (?, ?, ?, ?, ?, 'manual', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                request_hash,
                guild_id,
                discord_user_id,
                player.nickname,
                delta,
                actual_delta,
                previous,
                updated,
                actor_discord_user_id,
                str(actor_name or ""),
                str(reason or ""),
                occurred_at_value,
                now,
            ),
        )
        outbox_id = _derived_uuid("outbox", "balance.changed", event_id)
        _enqueue_outbox(
            connection,
            event_id=outbox_id,
            guild_id=guild_id,
            event_type="balance.changed",
            aggregate_type="balance_history",
            aggregate_id=event_id,
            payload={
                "event_id": event_id,
                "player": _player_payload(player),
                "previous_balance": previous,
                "requested_delta": delta,
                "actual_delta": actual_delta,
                "updated_balance": updated,
                "actor_discord_user_id": actor_discord_user_id,
                "actor_name": str(actor_name or ""),
                "reason": str(reason or ""),
                "occurred_at": occurred_at_value,
            },
            now=now,
        )
        history = connection.execute(
            "SELECT * FROM balance_history WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        return _balance_result_from_history(connection, history, replay=False)


def _lootsplit_result_from_database(
    connection: sqlite3.Connection,
    lootsplit_id: str,
    *,
    replay: bool,
) -> LootsplitResult:
    lootsplit = connection.execute(
        "SELECT * FROM lootsplits WHERE lootsplit_id = ?",
        (lootsplit_id,),
    ).fetchone()
    if lootsplit is None:
        raise RepositoryError(f"lootsplit {lootsplit_id} does not exist")
    participant_rows = connection.execute(
        """
        SELECT * FROM lootsplit_participants
        WHERE lootsplit_id = ?
        ORDER BY participant_index
        """,
        (lootsplit_id,),
    ).fetchall()
    credits = tuple(
        LootsplitCredit(
            discord_user_id=int(row["discord_user_id"]),
            nickname=str(row["nickname_snapshot"]),
            amount=int(row["amount"]),
            previous_balance=int(row["previous_balance"]),
            updated_balance=int(row["updated_balance"]),
            player_revision=int(row["player_revision"]),
            history_event_id=str(row["history_event_id"]),
        )
        for row in participant_rows
    )
    return LootsplitResult(
        lootsplit_id=lootsplit_id,
        credits=credits,
        missing_nicknames=tuple(
            _decode_list_json(
                lootsplit["missing_nicknames_json"],
                location=f"lootsplits/{lootsplit_id}/missing_nicknames",
            )
        ),
        idempotent_replay=replay,
        officer_discord_user_id=(
            None
            if lootsplit["officer_discord_user_id"] is None
            else int(lootsplit["officer_discord_user_id"])
        ),
        officer_name=str(lootsplit["officer_name"] or ""),
    )


def apply_lootsplit(
    guild_id: int,
    participants: Sequence[str],
    amount: int,
    *,
    battleboard_ids: Sequence[str] = (),
    actor_discord_user_id: Optional[int] = None,
    actor_name: str = "",
    officer_discord_user_id: Optional[int] = None,
    officer_name: str = "",
    content_name: str = "",
    caller_discord_user_id: Optional[int] = None,
    caller_name: str = "",
    idempotency_key: Optional[str] = None,
    occurred_at: Optional[str | datetime] = None,
    database_path: DatabasePath = None,
) -> LootsplitResult:
    """Credit every matched participant and record the entire batch atomically."""

    _validate_identifier(guild_id, "guild_id")
    amount = _validate_integer(amount, "amount")
    if amount < 0:
        raise ValueError("amount must not be negative")
    if actor_discord_user_id is not None:
        _validate_identifier(actor_discord_user_id, "actor_discord_user_id")
    if officer_discord_user_id is not None:
        _validate_identifier(officer_discord_user_id, "officer_discord_user_id")
    if caller_discord_user_id is not None:
        _validate_identifier(caller_discord_user_id, "caller_discord_user_id")
    if isinstance(participants, (str, bytes)):
        raise ValueError("participants must be a sequence of nicknames")
    if isinstance(battleboard_ids, (str, bytes)):
        raise ValueError("battleboard_ids must be a sequence of identifiers")
    clean_participants = tuple(_clean_nickname(name) for name in participants)
    if not clean_participants:
        raise ValueError("participants must not be empty")
    clean_battle_ids = tuple(
        value for value in (str(item or "").strip() for item in battleboard_ids) if value
    )
    occurred_at_value = _as_utc_string(occurred_at)
    lootsplit_id = _event_uuid(f"lootsplit:{guild_id}", idempotency_key)
    request = {
        "guild_id": guild_id,
        "participants": clean_participants,
        "amount": amount,
        "battleboard_ids": clean_battle_ids,
        "actor_discord_user_id": actor_discord_user_id,
        "actor_name": str(actor_name or ""),
        "officer_discord_user_id": officer_discord_user_id,
        "officer_name": str(officer_name or ""),
        "content_name": str(content_name or ""),
        "caller_discord_user_id": caller_discord_user_id,
        "caller_name": str(caller_name or ""),
        "occurred_at": occurred_at_value if occurred_at is not None else None,
    }
    request_hash = _request_hash(request)
    now = _utc_now()

    with _transaction(database_path) as connection:
        _require_active_ledger(connection, guild_id)
        existing = connection.execute(
            "SELECT request_hash FROM lootsplits WHERE lootsplit_id = ?",
            (lootsplit_id,),
        ).fetchone()
        if existing is not None:
            if existing["request_hash"] != request_hash:
                raise IdempotencyConflictError(
                    "lootsplit idempotency key was reused for a different request"
                )
            return _lootsplit_result_from_database(
                connection,
                lootsplit_id,
                replay=True,
            )

        player_rows = connection.execute(
            "SELECT * FROM registered_players WHERE guild_id = ?",
            (guild_id,),
        ).fetchall()
        players_by_key = {str(row["nickname_key"]): row for row in player_rows}
        missing: list[str] = []
        missing_keys: set[str] = set()
        resolved: list[tuple[int, str, sqlite3.Row]] = []
        for participant_index, participant in enumerate(clean_participants):
            key = normalize_nickname(participant)
            player_row = players_by_key.get(key)
            if player_row is None:
                if key not in missing_keys:
                    missing.append(participant)
                    missing_keys.add(key)
                continue
            resolved.append((participant_index, participant, player_row))

        connection.execute(
            """
            INSERT INTO lootsplits (
                lootsplit_id, guild_id, request_hash, battleboard_ids_json,
                actor_discord_user_id, actor_name,
                officer_discord_user_id, officer_name, content_name,
                caller_discord_user_id, caller_name, amount_per_participant,
                missing_nicknames_json, occurred_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lootsplit_id,
                guild_id,
                request_hash,
                _canonical_json(clean_battle_ids),
                actor_discord_user_id,
                str(actor_name or ""),
                officer_discord_user_id,
                str(officer_name or ""),
                str(content_name or ""),
                caller_discord_user_id,
                str(caller_name or ""),
                amount,
                _canonical_json(missing),
                occurred_at_value,
                now,
            ),
        )

        current_rows = {int(row["discord_user_id"]): row for row in player_rows}
        credits: list[LootsplitCredit] = []
        for participant_index, _requested_name, original_row in resolved:
            discord_user_id = int(original_row["discord_user_id"])
            current_row = current_rows[discord_user_id]
            previous = int(current_row["silver"])
            updated = previous + amount
            _validate_integer(updated, "updated_balance")
            all_time_earnings = int(current_row["all_time_earnings"]) + amount
            _validate_integer(all_time_earnings, "all_time_earnings")
            revision_increment = 1 if amount != 0 else 0
            next_revision = int(current_row["revision"]) + revision_increment
            connection.execute(
                """
                UPDATE registered_players
                SET silver = ?, all_time_earnings = ?, revision = ?,
                    siphon = CASE WHEN ? = 1 THEN NULL ELSE siphon END,
                    siphon_revision = CASE
                        WHEN ? = 1 THEN NULL ELSE siphon_revision
                    END,
                    siphon_synced_at = CASE
                        WHEN ? = 1 THEN NULL ELSE siphon_synced_at
                    END,
                    updated_at = ?
                WHERE guild_id = ? AND discord_user_id = ?
                """,
                (
                    updated,
                    all_time_earnings,
                    next_revision,
                    revision_increment,
                    revision_increment,
                    revision_increment,
                    now,
                    guild_id,
                    discord_user_id,
                ),
            )
            current_row = _require_player_row(
                connection,
                guild_id,
                discord_user_id,
            )
            current_rows[discord_user_id] = current_row
            nickname_snapshot = str(current_row["nickname"])
            history_event_id = _derived_uuid(
                "lootsplit.history",
                lootsplit_id,
                participant_index,
            )
            history_request_hash = _request_hash(
                {
                    "lootsplit_id": lootsplit_id,
                    "participant_index": participant_index,
                    "discord_user_id": discord_user_id,
                    "amount": amount,
                }
            )
            connection.execute(
                """
                INSERT INTO balance_history (
                    event_id, request_hash, guild_id, discord_user_id,
                    nickname_snapshot, event_kind, requested_delta,
                    actual_delta, previous_balance, updated_balance,
                    actor_discord_user_id, actor_name, reason, lootsplit_id,
                    occurred_at, created_at
                ) VALUES (?, ?, ?, ?, ?, 'lootsplit', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    history_event_id,
                    history_request_hash,
                    guild_id,
                    discord_user_id,
                    nickname_snapshot,
                    amount,
                    amount,
                    previous,
                    updated,
                    actor_discord_user_id,
                    str(actor_name or ""),
                    str(content_name or ""),
                    lootsplit_id,
                    occurred_at_value,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO lootsplit_participants (
                    lootsplit_id, participant_index, guild_id,
                    discord_user_id, nickname_snapshot, amount,
                    previous_balance, updated_balance, player_revision,
                    history_event_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lootsplit_id,
                    participant_index,
                    guild_id,
                    discord_user_id,
                    nickname_snapshot,
                    amount,
                    previous,
                    updated,
                    next_revision,
                    history_event_id,
                ),
            )
            credits.append(
                LootsplitCredit(
                    discord_user_id=discord_user_id,
                    nickname=nickname_snapshot,
                    amount=amount,
                    previous_balance=previous,
                    updated_balance=updated,
                    player_revision=next_revision,
                    history_event_id=history_event_id,
                )
            )

        outbox_id = _derived_uuid("outbox", "lootsplit.applied", lootsplit_id)
        _enqueue_outbox(
            connection,
            event_id=outbox_id,
            guild_id=guild_id,
            event_type="lootsplit.applied",
            aggregate_type="lootsplit",
            aggregate_id=lootsplit_id,
            payload={
                "event_id": lootsplit_id,
                "battleboard_ids": clean_battle_ids,
                "actor_discord_user_id": actor_discord_user_id,
                "actor_name": str(actor_name or ""),
                "officer_discord_user_id": officer_discord_user_id,
                "officer_name": str(officer_name or ""),
                "content_name": str(content_name or ""),
                "caller_discord_user_id": caller_discord_user_id,
                "caller_name": str(caller_name or ""),
                "amount_per_participant": amount,
                "occurred_at": occurred_at_value,
                "credits": [
                    {
                        "discord_user_id": credit.discord_user_id,
                        "nickname": credit.nickname,
                        "amount": credit.amount,
                        "previous_balance": credit.previous_balance,
                        "updated_balance": credit.updated_balance,
                        "player_revision": credit.player_revision,
                        "history_event_id": credit.history_event_id,
                    }
                    for credit in credits
                ],
                "missing_nicknames": missing,
            },
            now=now,
        )
        return LootsplitResult(
            lootsplit_id=lootsplit_id,
            credits=tuple(credits),
            missing_nicknames=tuple(missing),
            officer_discord_user_id=officer_discord_user_id,
            officer_name=str(officer_name or ""),
        )


def get_balance_snapshot(
    guild_id: int,
    discord_user_id: int,
    *,
    database_path: DatabasePath = None,
) -> Optional[BalanceSnapshot]:
    player = get_player(
        guild_id,
        discord_user_id,
        database_path=database_path,
    )
    if player is None:
        return None
    return BalanceSnapshot(
        guild_id=player.guild_id,
        discord_user_id=player.discord_user_id,
        nickname=player.nickname,
        silver=player.silver,
        all_time_earnings=player.all_time_earnings,
        revision=player.revision,
        siphon=player.siphon,
        siphon_revision=player.siphon_revision,
        siphon_synced_at=player.siphon_synced_at,
        is_active=player.is_active,
    )


def list_negative_siphon(
    guild_id: int,
    *,
    active_only: bool = False,
    max_age_seconds: Optional[int] = None,
    database_path: DatabasePath = None,
) -> list[BalanceSnapshot]:
    _validate_identifier(guild_id, "guild_id")
    _validate_boolean(active_only, "active_only")
    if max_age_seconds is not None and (
        isinstance(max_age_seconds, bool)
        or not isinstance(max_age_seconds, int)
        or max_age_seconds < 0
    ):
        raise ValueError("max_age_seconds must be a non-negative integer or None")
    active_clause = "AND is_active = 1" if active_only else ""
    freshness_clause = ""
    parameters: list[Any] = [guild_id]
    if max_age_seconds is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)).isoformat(
            timespec="microseconds"
        )
        freshness_clause = "AND siphon_synced_at >= ?"
        parameters.append(cutoff)
    with _connection(database_path) as connection:
        rows = connection.execute(
            f"""
            SELECT * FROM registered_players
            WHERE guild_id = ?
                AND siphon < 0
                AND siphon_revision = revision
                AND siphon_synced_at IS NOT NULL
                {active_clause}
                {freshness_clause}
            ORDER BY siphon ASC, nickname_key, discord_user_id
            """,
            parameters,
        ).fetchall()
        return [
            BalanceSnapshot(
                guild_id=int(row["guild_id"]),
                discord_user_id=int(row["discord_user_id"]),
                nickname=str(row["nickname"]),
                silver=int(row["silver"]),
                all_time_earnings=int(row["all_time_earnings"]),
                revision=int(row["revision"]),
                siphon=int(row["siphon"]),
                siphon_revision=int(row["siphon_revision"]),
                siphon_synced_at=row["siphon_synced_at"],
                is_active=bool(row["is_active"]),
            )
            for row in rows
        ]


def cache_siphon(
    guild_id: int,
    discord_user_id: int,
    siphon: Optional[int],
    *,
    expected_revision: Optional[int] = None,
    synced_at: Optional[str | datetime] = None,
    database_path: DatabasePath = None,
) -> SiphonCacheResult:
    """Cache a Sheet-calculated Siphon only for the expected local revision."""

    _validate_identifier(guild_id, "guild_id")
    _validate_identifier(discord_user_id, "discord_user_id")
    if siphon is not None:
        siphon = _validate_integer(siphon, "siphon")
    if expected_revision is not None:
        expected_revision = _validate_integer(expected_revision, "expected_revision")
        if expected_revision < 1:
            raise ValueError("expected_revision must be positive")
    now = _utc_now()
    synchronized_at = _as_utc_string(synced_at) if siphon is not None else None
    with _transaction(database_path) as connection:
        _require_active_ledger(connection, guild_id)
        return _cache_siphon_row(
            connection,
            guild_id=guild_id,
            discord_user_id=discord_user_id,
            siphon=siphon,
            expected_revision=expected_revision,
            synchronized_at=synchronized_at,
            now=now,
        )


def _cache_siphon_row(
    connection: sqlite3.Connection,
    *,
    guild_id: int,
    discord_user_id: int,
    siphon: Optional[int],
    expected_revision: Optional[int],
    synchronized_at: Optional[str],
    now: str,
) -> SiphonCacheResult:
    row = _get_player_row(connection, guild_id, discord_user_id)
    if row is None:
        return SiphonCacheResult(SiphonCacheStatus.NOT_FOUND, None)
    if expected_revision is not None and int(row["revision"]) != expected_revision:
        return SiphonCacheResult(
            SiphonCacheStatus.STALE_REVISION,
            _row_to_player(row),
        )
    connection.execute(
        """
        UPDATE registered_players
        SET siphon = ?, siphon_revision = ?, siphon_synced_at = ?, updated_at = ?
        WHERE guild_id = ? AND discord_user_id = ?
        """,
        (
            siphon,
            int(row["revision"]) if siphon is not None else None,
            synchronized_at,
            now,
            guild_id,
            discord_user_id,
        ),
    )
    updated = _require_player_row(connection, guild_id, discord_user_id)
    return SiphonCacheResult(
        SiphonCacheStatus.UPDATED,
        _row_to_player(updated),
    )


def cache_siphons(
    guild_id: int,
    entries: Sequence[SiphonUpdate],
    *,
    synced_at: Optional[str | datetime] = None,
    replace_snapshot: bool = False,
    database_path: DatabasePath = None,
) -> list[SiphonCacheResult]:
    """Cache one Sheet snapshot in a single local transaction.

    Individual rows with stale revisions are reported and skipped; accepted
    rows become visible together when the transaction commits. A replacement
    snapshot first invalidates every cached value in the guild, so omitted or
    rejected rows cannot retain values from an older Sheet snapshot.
    """

    _validate_identifier(guild_id, "guild_id")
    _validate_boolean(replace_snapshot, "replace_snapshot")
    synchronized_at = _as_utc_string(synced_at)
    now = _utc_now()
    validated_entries: list[SiphonUpdate] = []
    seen_discord_ids: set[int] = set()
    for entry in entries:
        if not isinstance(entry, SiphonUpdate):
            raise ValueError("entries must contain SiphonUpdate values")
        _validate_identifier(entry.discord_user_id, "discord_user_id")
        _validate_integer(entry.expected_revision, "expected_revision")
        if entry.expected_revision < 1:
            raise ValueError("expected_revision must be positive")
        if entry.siphon is not None:
            _validate_integer(entry.siphon, "siphon")
        if entry.discord_user_id in seen_discord_ids:
            raise ValueError("a Siphon snapshot must not contain duplicate users")
        seen_discord_ids.add(entry.discord_user_id)
        validated_entries.append(entry)

    with _transaction(database_path) as connection:
        _require_active_ledger(connection, guild_id)
        if replace_snapshot:
            connection.execute(
                """
                UPDATE registered_players
                SET siphon = NULL, siphon_revision = NULL,
                    siphon_synced_at = NULL, updated_at = ?
                WHERE guild_id = ?
                    AND (
                        siphon IS NOT NULL
                        OR siphon_revision IS NOT NULL
                        OR siphon_synced_at IS NOT NULL
                    )
                """,
                (now, guild_id),
            )
        return [
            _cache_siphon_row(
                connection,
                guild_id=guild_id,
                discord_user_id=entry.discord_user_id,
                siphon=entry.siphon,
                expected_revision=entry.expected_revision,
                synchronized_at=(synchronized_at if entry.siphon is not None else None),
                now=now,
            )
            for entry in validated_entries
        ]


def import_balance_history(
    guild_id: int,
    nickname: str,
    amount: int,
    *,
    source_key: str,
    occurred_at: str | datetime,
    reason: str = "",
    actor_name: str = "",
    database_path: DatabasePath = None,
) -> HistoryImportResult:
    """Persist one legacy Sheet balance row without changing current Silver."""

    _validate_identifier(guild_id, "guild_id")
    clean_nickname = _clean_nickname(nickname)
    amount = _validate_integer(amount, "amount")
    clean_source_key = str(source_key or "").strip()
    if not clean_source_key:
        raise ValueError("source_key must not be blank")
    occurred_at_value = _as_utc_string(occurred_at)
    event_id = _derived_uuid(
        "sheet.balance_history",
        guild_id,
        clean_source_key,
    )
    request = {
        "guild_id": guild_id,
        "nickname": clean_nickname,
        "amount": amount,
        "source_key": clean_source_key,
        "occurred_at": occurred_at_value,
        "reason": str(reason or ""),
        "actor_name": str(actor_name or ""),
    }
    request_hash = _request_hash(request)
    now = _utc_now()

    with _transaction(database_path) as connection:
        _require_active_ledger(connection, guild_id)
        existing = connection.execute(
            "SELECT * FROM balance_history WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if existing is not None:
            if existing["request_hash"] != request_hash:
                raise IdempotencyConflictError(
                    "balance history source_key was reused for a different row"
                )
            player_row = (
                None
                if existing["discord_user_id"] is None
                else _get_player_row(
                    connection,
                    guild_id,
                    int(existing["discord_user_id"]),
                )
            )
            return HistoryImportResult(
                HistoryImportStatus.ALREADY_IMPORTED,
                event_id,
                None if player_row is None else _row_to_player(player_row),
            )

        player_row = connection.execute(
            """
            SELECT * FROM registered_players
            WHERE guild_id = ? AND nickname_key = ?
            """,
            (guild_id, normalize_nickname(clean_nickname)),
        ).fetchone()
        discord_user_id = None if player_row is None else int(player_row["discord_user_id"])
        connection.execute(
            """
            INSERT INTO balance_history (
                event_id, request_hash, guild_id, discord_user_id,
                nickname_snapshot, event_kind, requested_delta, actual_delta,
                previous_balance, updated_balance, actor_name, reason,
                occurred_at, created_at
            ) VALUES (?, ?, ?, ?, ?, 'sheet_import', ?, ?, NULL, NULL, ?, ?, ?, ?)
            """,
            (
                event_id,
                request_hash,
                guild_id,
                discord_user_id,
                clean_nickname,
                amount,
                amount,
                str(actor_name or ""),
                str(reason or ""),
                occurred_at_value,
                now,
            ),
        )
        if player_row is not None and amount > 0:
            target_discord_user_id = int(player_row["discord_user_id"])
            all_time_earnings = int(player_row["all_time_earnings"]) + amount
            _validate_integer(all_time_earnings, "all_time_earnings")
            connection.execute(
                """
                UPDATE registered_players
                SET all_time_earnings = ?, updated_at = ?
                WHERE guild_id = ? AND discord_user_id = ?
                """,
                (all_time_earnings, now, guild_id, target_discord_user_id),
            )
            player_row = _require_player_row(
                connection,
                guild_id,
                target_discord_user_id,
            )
        return HistoryImportResult(
            HistoryImportStatus.IMPORTED,
            event_id,
            None if player_row is None else _row_to_player(player_row),
        )


def import_lootsplit_history(
    guild_id: int,
    nickname: str,
    amount: int,
    *,
    source_key: str,
    occurred_at: str | datetime,
    battleboard_ids: Sequence[str] = (),
    actor_name: str = "",
    content_name: str = "",
    caller_name: str = "",
    database_path: DatabasePath = None,
) -> HistoryImportResult:
    """Persist one legacy Sheet lootsplit row without replaying its credit."""

    _validate_identifier(guild_id, "guild_id")
    clean_nickname = _clean_nickname(nickname)
    amount = _validate_integer(amount, "amount")
    clean_source_key = str(source_key or "").strip()
    if not clean_source_key:
        raise ValueError("source_key must not be blank")
    if isinstance(battleboard_ids, (str, bytes)):
        raise ValueError("battleboard_ids must be a sequence of identifiers")
    clean_battle_ids = tuple(
        value for value in (str(item or "").strip() for item in battleboard_ids) if value
    )
    occurred_at_value = _as_utc_string(occurred_at)
    event_id = _derived_uuid(
        "sheet.lootsplit_history",
        guild_id,
        clean_source_key,
    )
    request = {
        "guild_id": guild_id,
        "nickname": clean_nickname,
        "amount": amount,
        "source_key": clean_source_key,
        "occurred_at": occurred_at_value,
        "battleboard_ids": clean_battle_ids,
        "actor_name": str(actor_name or ""),
        "content_name": str(content_name or ""),
        "caller_name": str(caller_name or ""),
    }
    request_hash = _request_hash(request)
    now = _utc_now()

    with _transaction(database_path) as connection:
        _require_active_ledger(connection, guild_id)
        existing = connection.execute(
            "SELECT * FROM imported_lootsplit_history WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if existing is not None:
            if existing["request_hash"] != request_hash:
                raise IdempotencyConflictError(
                    "lootsplit history source_key was reused for a different row"
                )
            player_row = (
                None
                if existing["discord_user_id"] is None
                else _get_player_row(
                    connection,
                    guild_id,
                    int(existing["discord_user_id"]),
                )
            )
            return HistoryImportResult(
                HistoryImportStatus.ALREADY_IMPORTED,
                event_id,
                None if player_row is None else _row_to_player(player_row),
            )

        player_row = connection.execute(
            """
            SELECT * FROM registered_players
            WHERE guild_id = ? AND nickname_key = ?
            """,
            (guild_id, normalize_nickname(clean_nickname)),
        ).fetchone()
        discord_user_id = None if player_row is None else int(player_row["discord_user_id"])
        connection.execute(
            """
            INSERT INTO imported_lootsplit_history (
                event_id, request_hash, guild_id, discord_user_id,
                nickname_snapshot, battleboard_ids_json, amount, actor_name,
                content_name, caller_name, source_key, occurred_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                request_hash,
                guild_id,
                discord_user_id,
                clean_nickname,
                _canonical_json(clean_battle_ids),
                amount,
                str(actor_name or ""),
                str(content_name or ""),
                str(caller_name or ""),
                clean_source_key,
                occurred_at_value,
                now,
            ),
        )
        return HistoryImportResult(
            HistoryImportStatus.IMPORTED,
            event_id,
            None if player_row is None else _row_to_player(player_row),
        )


def _row_to_outbox(row: sqlite3.Row) -> OutboxEvent:
    return OutboxEvent(
        event_id=str(row["event_id"]),
        guild_id=int(row["guild_id"]),
        event_type=str(row["event_type"]),
        aggregate_type=str(row["aggregate_type"]),
        aggregate_id=str(row["aggregate_id"]),
        payload=_decode_mapping_json(
            row["payload_json"],
            location=f"google_sync_outbox/{row['event_id']}",
        ),
        status=str(row["status"]),
        attempts=int(row["attempts"]),
        available_at=str(row["available_at"]),
        created_at=str(row["created_at"]),
        last_error=row["last_error"],
        lease_owner=row["lease_owner"],
        lease_until=row["lease_until"],
    )


def list_pending_outbox(
    *,
    limit: int = 100,
    guild_id: Optional[int] = None,
    now: Optional[str | datetime] = None,
    database_path: DatabasePath = None,
) -> list[OutboxEvent]:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    if guild_id is not None:
        _validate_identifier(guild_id, "guild_id")
    now_value = _as_utc_string(now)
    guild_clause = "AND guild_id = ?" if guild_id is not None else ""
    parameters: list[Any] = [now_value, now_value]
    if guild_id is not None:
        parameters.append(guild_id)
    parameters.append(limit)
    with _connection(database_path) as connection:
        rows = connection.execute(
            f"""
            SELECT * FROM google_sync_outbox
            WHERE (
                (status = 'pending' AND available_at <= ?)
                OR (status = 'processing' AND lease_until <= ?)
            )
            {guild_clause}
            ORDER BY created_at, event_id
            LIMIT ?
            """,
            parameters,
        ).fetchall()
        return [_row_to_outbox(row) for row in rows]


def has_incomplete_outbox(
    *,
    guild_id: int,
    database_path: DatabasePath = None,
) -> bool:
    """Return whether a guild still has pending or leased projection work."""

    _validate_identifier(guild_id, "guild_id")
    with _connection(database_path) as connection:
        return (
            connection.execute(
                """
            SELECT EXISTS (
                SELECT 1 FROM google_sync_outbox
                WHERE guild_id = ? AND status != 'completed'
            )
            """,
                (guild_id,),
            ).fetchone()[0]
            == 1
        )


def get_outbox_status(
    guild_id: int,
    *,
    database_path: DatabasePath = None,
) -> OutboxStatus:
    """Return one consistent queue/dead-letter snapshot for operator tooling."""

    _validate_identifier(guild_id, "guild_id")
    with _connection(database_path) as connection:
        counts = connection.execute(
            """
            SELECT
                COALESCE(SUM(status = 'pending'), 0) AS pending_events,
                COALESCE(SUM(status = 'processing'), 0) AS processing_events,
                COALESCE(SUM(status = 'completed'), 0) AS completed_events,
                MIN(CASE WHEN status != 'completed' THEN created_at END)
                    AS oldest_incomplete_at,
                MAX(CASE WHEN status = 'completed' THEN completed_at END)
                    AS last_completed_at
            FROM google_sync_outbox WHERE guild_id = ?
            """,
            (guild_id,),
        ).fetchone()
        dead_stats = connection.execute(
            """
            SELECT COUNT(*) AS dead_count, MIN(created_at) AS oldest_created_at
            FROM google_sync_dead_letters WHERE guild_id = ?
            """,
            (guild_id,),
        ).fetchone()
        oldest_candidates = [
            value
            for value in (
                counts["oldest_incomplete_at"],
                dead_stats["oldest_created_at"],
            )
            if value is not None
        ]
        oldest_incomplete_at = min(oldest_candidates) if oldest_candidates else None
        error_row = connection.execute(
            """
            SELECT last_error FROM (
                SELECT last_error, created_at AS error_at, event_id
                FROM google_sync_outbox
                WHERE guild_id = ? AND last_error IS NOT NULL
                UNION ALL
                SELECT last_error, dead_lettered_at AS error_at, event_id
                FROM google_sync_dead_letters
                WHERE guild_id = ? AND last_error IS NOT NULL
            )
            ORDER BY error_at DESC, event_id DESC LIMIT 1
            """,
            (guild_id, guild_id),
        ).fetchone()
    return OutboxStatus(
        guild_id=guild_id,
        pending_events=int(counts["pending_events"]),
        processing_events=int(counts["processing_events"]),
        completed_events=int(counts["completed_events"]),
        dead_letter_events=int(dead_stats["dead_count"]),
        oldest_incomplete_at=oldest_incomplete_at,
        last_completed_at=counts["last_completed_at"],
        latest_error=None if error_row is None else error_row["last_error"],
    )


def claim_pending_outbox(
    worker_id: str,
    *,
    limit: int = 100,
    lease_seconds: int = 60,
    guild_id: Optional[int] = None,
    now: Optional[str | datetime] = None,
    database_path: DatabasePath = None,
) -> list[OutboxEvent]:
    """Lease at most one ready head event per guild in strict FIFO order.

    A pending retry or an unexpired lease blocks every later event for that
    guild. This prevents concurrent projectors from applying a newer player
    snapshot before an older one and then overwriting it out of order.
    """

    clean_worker_id = str(worker_id or "").strip()
    if not clean_worker_id:
        raise ValueError("worker_id must not be blank")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds <= 0:
        raise ValueError("lease_seconds must be a positive integer")
    if guild_id is not None:
        _validate_identifier(guild_id, "guild_id")
    now_value = _as_utc_string(now)
    lease_until = (
        datetime.fromisoformat(now_value).astimezone(timezone.utc)
        + timedelta(seconds=lease_seconds)
    ).isoformat(timespec="microseconds")
    guild_clause = "AND guild_id = ?" if guild_id is not None else ""
    select_parameters: list[Any] = [now_value, now_value]
    if guild_id is not None:
        select_parameters.append(guild_id)
    select_parameters.append(limit)

    with _transaction(database_path) as connection:
        rows = connection.execute(
            f"""
            SELECT candidate.event_id
            FROM google_sync_outbox AS candidate
            WHERE candidate.status != 'completed'
            AND (
                (candidate.status = 'pending' AND candidate.available_at <= ?)
                OR (
                    candidate.status = 'processing'
                    AND (
                        candidate.lease_until IS NULL
                        OR candidate.lease_until <= ?
                    )
                )
            )
            {guild_clause.replace("guild_id", "candidate.guild_id")}
            AND NOT EXISTS (
                SELECT 1
                FROM google_sync_outbox AS predecessor
                WHERE predecessor.guild_id = candidate.guild_id
                    AND predecessor.status != 'completed'
                    AND (
                        predecessor.created_at < candidate.created_at
                        OR (
                            predecessor.created_at = candidate.created_at
                            AND predecessor.event_id < candidate.event_id
                        )
                    )
            )
            ORDER BY candidate.created_at, candidate.event_id
            LIMIT ?
            """,
            select_parameters,
        ).fetchall()
        event_ids = [str(row["event_id"]) for row in rows]
        if not event_ids:
            return []
        placeholders = ",".join("?" for _ in event_ids)
        connection.execute(
            f"""
            UPDATE google_sync_outbox
            SET status = 'processing', lease_owner = ?, lease_until = ?
            WHERE event_id IN ({placeholders})
            """,
            [clean_worker_id, lease_until, *event_ids],
        )
        claimed_rows = connection.execute(
            f"""
            SELECT * FROM google_sync_outbox
            WHERE event_id IN ({placeholders})
            ORDER BY created_at, event_id
            """,
            event_ids,
        ).fetchall()
        return [_row_to_outbox(row) for row in claimed_rows]


def ack_outbox(
    event_id: str,
    *,
    worker_id: Optional[str] = None,
    database_path: DatabasePath = None,
) -> bool:
    clean_event_id = str(event_id or "").strip()
    if not clean_event_id:
        raise ValueError("event_id must not be blank")
    now = _utc_now()
    with _transaction(database_path) as connection:
        if worker_id is None:
            cursor = connection.execute(
                """
                UPDATE google_sync_outbox
                SET status = 'completed', completed_at = ?, lease_owner = NULL,
                    lease_until = NULL, last_error = NULL
                WHERE event_id = ? AND status != 'completed'
                """,
                (now, clean_event_id),
            )
        else:
            clean_worker_id = str(worker_id or "").strip()
            if not clean_worker_id:
                raise ValueError("worker_id must not be blank")
            cursor = connection.execute(
                """
                UPDATE google_sync_outbox
                SET status = 'completed', completed_at = ?, lease_owner = NULL,
                    lease_until = NULL, last_error = NULL
                WHERE event_id = ? AND status = 'processing'
                    AND lease_owner = ?
                """,
                (now, clean_event_id, clean_worker_id),
            )
        return cursor.rowcount == 1


def fail_outbox(
    event_id: str,
    error: str,
    *,
    retry_after_seconds: int = 60,
    worker_id: Optional[str] = None,
    database_path: DatabasePath = None,
) -> bool:
    clean_event_id = str(event_id or "").strip()
    if not clean_event_id:
        raise ValueError("event_id must not be blank")
    if (
        isinstance(retry_after_seconds, bool)
        or not isinstance(retry_after_seconds, int)
        or retry_after_seconds < 0
    ):
        raise ValueError("retry_after_seconds must be a non-negative integer")
    available_at = (datetime.now(timezone.utc) + timedelta(seconds=retry_after_seconds)).isoformat(
        timespec="microseconds"
    )
    worker_clause = "AND lease_owner = ?" if worker_id is not None else ""
    parameters: list[Any] = [available_at, str(error or "")[:2000], clean_event_id]
    if worker_id is not None:
        clean_worker_id = str(worker_id or "").strip()
        if not clean_worker_id:
            raise ValueError("worker_id must not be blank")
        parameters.append(clean_worker_id)
    with _transaction(database_path) as connection:
        cursor = connection.execute(
            f"""
            UPDATE google_sync_outbox
            SET status = 'pending', attempts = attempts + 1,
                available_at = ?, last_error = ?, lease_owner = NULL,
                lease_until = NULL
            WHERE event_id = ? AND status != 'completed' {worker_clause}
            """,
            parameters,
        )
        return cursor.rowcount == 1


def _row_to_dead_letter(row: sqlite3.Row) -> DeadLetterEvent:
    return DeadLetterEvent(
        event_id=str(row["event_id"]),
        guild_id=int(row["guild_id"]),
        event_type=str(row["event_type"]),
        aggregate_type=str(row["aggregate_type"]),
        aggregate_id=str(row["aggregate_id"]),
        payload=_decode_mapping_json(
            row["payload_json"],
            location=f"google_sync_dead_letters/{row['event_id']}",
        ),
        attempts=int(row["attempts"]),
        available_at=str(row["available_at"]),
        created_at=str(row["created_at"]),
        last_error=row["last_error"],
        dead_lettered_at=str(row["dead_lettered_at"]),
    )


def dead_letter_outbox(
    event_id: str,
    error: str,
    *,
    worker_id: Optional[str] = None,
    database_path: DatabasePath = None,
) -> bool:
    """Move one poison event aside atomically so its guild FIFO can progress."""

    clean_event_id = str(event_id or "").strip()
    if not clean_event_id:
        raise ValueError("event_id must not be blank")
    clean_worker_id = None
    if worker_id is not None:
        clean_worker_id = str(worker_id or "").strip()
        if not clean_worker_id:
            raise ValueError("worker_id must not be blank")
    now = _utc_now()
    with _transaction(database_path) as connection:
        row = connection.execute(
            "SELECT * FROM google_sync_outbox WHERE event_id = ?",
            (clean_event_id,),
        ).fetchone()
        if row is None or str(row["status"]) == "completed":
            return False
        if clean_worker_id is not None and (
            str(row["status"]) != "processing" or str(row["lease_owner"] or "") != clean_worker_id
        ):
            return False
        connection.execute(
            """
            INSERT INTO google_sync_dead_letters (
                event_id, guild_id, event_type, aggregate_type, aggregate_id,
                payload_json, attempts, available_at, last_error, created_at,
                dead_lettered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (event_id) DO NOTHING
            """,
            (
                clean_event_id,
                int(row["guild_id"]),
                str(row["event_type"]),
                str(row["aggregate_type"]),
                str(row["aggregate_id"]),
                str(row["payload_json"]),
                int(row["attempts"]) + 1,
                str(row["available_at"]),
                str(error or "")[:2000],
                str(row["created_at"]),
                now,
            ),
        )
        cursor = connection.execute(
            "DELETE FROM google_sync_outbox WHERE event_id = ?",
            (clean_event_id,),
        )
        return cursor.rowcount == 1


def list_dead_letter_outbox(
    *,
    guild_id: Optional[int] = None,
    limit: int = 100,
    database_path: DatabasePath = None,
) -> list[DeadLetterEvent]:
    if guild_id is not None:
        _validate_identifier(guild_id, "guild_id")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    guild_clause = "WHERE guild_id = ?" if guild_id is not None else ""
    parameters: list[Any] = [] if guild_id is None else [guild_id]
    parameters.append(limit)
    with _connection(database_path) as connection:
        rows = connection.execute(
            f"""
            SELECT * FROM google_sync_dead_letters {guild_clause}
            ORDER BY dead_lettered_at, event_id LIMIT ?
            """,
            parameters,
        ).fetchall()
        return [_row_to_dead_letter(row) for row in rows]


def has_dead_letter_outbox(
    *,
    guild_id: int,
    database_path: DatabasePath = None,
) -> bool:
    _validate_identifier(guild_id, "guild_id")
    with _connection(database_path) as connection:
        return (
            connection.execute(
                "SELECT EXISTS (SELECT 1 FROM google_sync_dead_letters WHERE guild_id = ?)",
                (guild_id,),
            ).fetchone()[0]
            == 1
        )


def retry_dead_letter_outbox(
    event_id: str,
    *,
    reset_attempts: bool = True,
    database_path: DatabasePath = None,
) -> bool:
    """Restore a quarantined event to the pending FIFO in one transaction."""

    clean_event_id = str(event_id or "").strip()
    if not clean_event_id:
        raise ValueError("event_id must not be blank")
    _validate_boolean(reset_attempts, "reset_attempts")
    now = _utc_now()
    with _transaction(database_path) as connection:
        row = connection.execute(
            "SELECT * FROM google_sync_dead_letters WHERE event_id = ?",
            (clean_event_id,),
        ).fetchone()
        if row is None:
            return False
        connection.execute(
            """
            INSERT INTO google_sync_outbox (
                event_id, guild_id, event_type, aggregate_type, aggregate_id,
                payload_json, status, attempts, available_at, last_error,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
            """,
            (
                clean_event_id,
                int(row["guild_id"]),
                str(row["event_type"]),
                str(row["aggregate_type"]),
                str(row["aggregate_id"]),
                str(row["payload_json"]),
                0 if reset_attempts else int(row["attempts"]),
                now,
                str(row["last_error"] or "")[:2000],
                str(row["created_at"]),
            ),
        )
        connection.execute(
            "DELETE FROM google_sync_dead_letters WHERE event_id = ?",
            (clean_event_id,),
        )
        return True


def retry_dead_letter_outbox_for_guild(
    guild_id: int,
    *,
    limit: int = 100,
    reset_attempts: bool = True,
    database_path: DatabasePath = None,
) -> int:
    """Restore a bounded batch of one guild's dead letters in original order."""

    _validate_identifier(guild_id, "guild_id")
    _validate_boolean(reset_attempts, "reset_attempts")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    now = _utc_now()
    with _transaction(database_path) as connection:
        rows = connection.execute(
            """
            SELECT * FROM google_sync_dead_letters
            WHERE guild_id = ?
            ORDER BY created_at, event_id LIMIT ?
            """,
            (guild_id, limit),
        ).fetchall()
        for row in rows:
            connection.execute(
                """
                INSERT INTO google_sync_outbox (
                    event_id, guild_id, event_type, aggregate_type, aggregate_id,
                    payload_json, status, attempts, available_at, last_error,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                """,
                (
                    str(row["event_id"]),
                    int(row["guild_id"]),
                    str(row["event_type"]),
                    str(row["aggregate_type"]),
                    str(row["aggregate_id"]),
                    str(row["payload_json"]),
                    0 if reset_attempts else int(row["attempts"]),
                    now,
                    str(row["last_error"] or "")[:2000],
                    str(row["created_at"]),
                ),
            )
        if rows:
            connection.executemany(
                "DELETE FROM google_sync_dead_letters WHERE event_id = ?",
                [(str(row["event_id"]),) for row in rows],
            )
        return len(rows)


def prune_completed_outbox(
    completed_before: str | datetime,
    *,
    guild_id: Optional[int] = None,
    limit: int = 1_000,
    database_path: DatabasePath = None,
) -> int:
    cutoff = _as_utc_string(completed_before)
    if guild_id is not None:
        _validate_identifier(guild_id, "guild_id")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    guild_clause = "AND guild_id = ?" if guild_id is not None else ""
    parameters: list[Any] = [cutoff]
    if guild_id is not None:
        parameters.append(guild_id)
    parameters.append(limit)
    with _transaction(database_path) as connection:
        cursor = connection.execute(
            f"""
            DELETE FROM google_sync_outbox
            WHERE event_id IN (
                SELECT event_id FROM google_sync_outbox
                WHERE status = 'completed' AND completed_at < ?
                {guild_clause}
                ORDER BY completed_at, event_id LIMIT ?
            )
            """,
            parameters,
        )
        return max(cursor.rowcount, 0)


def prune_applied_sheet_snapshots(
    applied_before: str | datetime,
    *,
    guild_id: Optional[int] = None,
    limit: int = 100,
    database_path: DatabasePath = None,
) -> int:
    cutoff = _as_utc_string(applied_before)
    if guild_id is not None:
        _validate_identifier(guild_id, "guild_id")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    guild_clause = "AND guild_id = ?" if guild_id is not None else ""
    parameters: list[Any] = [cutoff]
    if guild_id is not None:
        parameters.append(guild_id)
    parameters.append(limit)
    with _transaction(database_path) as connection:
        cursor = connection.execute(
            f"""
            DELETE FROM sheet_import_snapshots
            WHERE snapshot_id IN (
                SELECT snapshot_id FROM sheet_import_snapshots
                WHERE status = 'applied' AND applied_at < ?
                {guild_clause}
                ORDER BY applied_at, snapshot_id LIMIT ?
            )
            """,
            parameters,
        )
        return max(cursor.rowcount, 0)


def _row_to_sheet_import(row: sqlite3.Row) -> SheetImport:
    return SheetImport(
        import_id=str(row["import_id"]),
        guild_id=int(row["guild_id"]),
        source_name=str(row["source_name"]),
        source_fingerprint=str(row["source_fingerprint"]),
        status=SheetImportStatus(row["status"]),
        metadata=_decode_mapping_json(
            row["metadata_json"],
            location=f"sheet_imports/{row['import_id']}/metadata",
        ),
        started_at=str(row["started_at"]),
        completed_at=row["completed_at"],
        row_count=int(row["row_count"]),
        error=row["error"],
    )


def _row_to_sheet_snapshot(row: sqlite3.Row) -> SheetImportSnapshot:
    return SheetImportSnapshot(
        snapshot_id=str(row["snapshot_id"]),
        guild_id=int(row["guild_id"]),
        source_name=str(row["source_name"]),
        source_fingerprint=str(row["source_fingerprint"]),
        status=SheetSnapshotStatus(row["status"]),
        metadata=_decode_mapping_json(
            row["metadata_json"],
            location=f"sheet_import_snapshots/{row['snapshot_id']}/metadata",
        ),
        snapshot=_decode_mapping_json(
            row["snapshot_json"],
            location=f"sheet_import_snapshots/{row['snapshot_id']}/snapshot",
        ),
        created_at=str(row["created_at"]),
        applied_at=row["applied_at"],
    )


def has_completed_sheet_import(
    guild_id: int,
    source_name: str,
    database_path: DatabasePath = None,
) -> bool:
    """Return whether this guild completed at least one import from a source."""

    _validate_identifier(guild_id, "guild_id")
    clean_source_name = str(source_name or "").strip()
    if not clean_source_name:
        raise ValueError("source_name must not be blank")
    with _connection(database_path) as connection:
        return (
            connection.execute(
                """
            SELECT EXISTS (
                SELECT 1 FROM sheet_imports
                WHERE guild_id = ? AND source_name = ? AND status = 'completed'
            )
            """,
                (guild_id, clean_source_name),
            ).fetchone()[0]
            == 1
        )


def get_latest_completed_sheet_import(
    guild_id: int,
    source_name: str,
    database_path: DatabasePath = None,
) -> Optional[SheetImport]:
    _validate_identifier(guild_id, "guild_id")
    clean_source_name = str(source_name or "").strip()
    if not clean_source_name:
        raise ValueError("source_name must not be blank")
    with _connection(database_path) as connection:
        row = connection.execute(
            """
            SELECT * FROM sheet_imports
            WHERE guild_id = ? AND source_name = ? AND status = 'completed'
            ORDER BY completed_at DESC, started_at DESC, import_id DESC
            LIMIT 1
            """,
            (guild_id, clean_source_name),
        ).fetchone()
        return None if row is None else _row_to_sheet_import(row)


def get_staged_sheet_snapshot(
    guild_id: int,
    source_name: str,
    *,
    database_path: DatabasePath = None,
) -> Optional[SheetImportSnapshot]:
    _validate_identifier(guild_id, "guild_id")
    clean_source = str(source_name or "").strip()
    if not clean_source:
        raise ValueError("source_name must not be blank")
    with _connection(database_path) as connection:
        row = connection.execute(
            """
            SELECT * FROM sheet_import_snapshots
            WHERE guild_id = ? AND source_name = ? AND status = 'staged'
            ORDER BY created_at, snapshot_id LIMIT 1
            """,
            (guild_id, clean_source),
        ).fetchone()
        return None if row is None else _row_to_sheet_snapshot(row)


def stage_sheet_snapshot(
    guild_id: int,
    source_name: str,
    source_fingerprint: str,
    snapshot: Mapping[str, Any],
    *,
    metadata: Optional[Mapping[str, Any]] = None,
    database_path: DatabasePath = None,
) -> SheetImportSnapshot:
    """Durably freeze a complete remote snapshot before importing any row."""

    _validate_identifier(guild_id, "guild_id")
    clean_source = str(source_name or "").strip()
    clean_fingerprint = str(source_fingerprint or "").strip()
    if not clean_source or not clean_fingerprint:
        raise ValueError("source_name and source_fingerprint must not be blank")
    if not isinstance(snapshot, Mapping):
        raise TypeError("snapshot must be a mapping")
    snapshot_id = _derived_uuid(
        "sheet.snapshot",
        guild_id,
        clean_source,
        clean_fingerprint,
    )
    metadata_json = _canonical_json(dict(metadata or {}))
    snapshot_json = _canonical_json(dict(snapshot))
    now = _utc_now()
    with _transaction(database_path) as connection:
        _require_active_ledger(connection, guild_id)
        staged = connection.execute(
            """
            SELECT * FROM sheet_import_snapshots
            WHERE guild_id = ? AND source_name = ? AND status = 'staged'
            """,
            (guild_id, clean_source),
        ).fetchone()
        if staged is not None and str(staged["snapshot_id"]) != snapshot_id:
            raise IdempotencyConflictError(
                "a different immutable snapshot is already staged for this import"
            )
        existing = connection.execute(
            "SELECT * FROM sheet_import_snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        if existing is not None:
            if (
                existing["metadata_json"] != metadata_json
                or existing["snapshot_json"] != snapshot_json
            ):
                raise IdempotencyConflictError(
                    "sheet snapshot fingerprint was reused with different content"
                )
            return _row_to_sheet_snapshot(existing)
        connection.execute(
            """
            INSERT INTO sheet_import_snapshots (
                snapshot_id, guild_id, source_name, source_fingerprint,
                status, metadata_json, snapshot_json, created_at
            ) VALUES (?, ?, ?, ?, 'staged', ?, ?, ?)
            """,
            (
                snapshot_id,
                guild_id,
                clean_source,
                clean_fingerprint,
                metadata_json,
                snapshot_json,
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM sheet_import_snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        return _row_to_sheet_snapshot(row)


def begin_sheet_import(
    guild_id: int,
    source_name: str,
    source_fingerprint: str,
    *,
    metadata: Optional[Mapping[str, Any]] = None,
    database_path: DatabasePath = None,
) -> SheetImport:
    _validate_identifier(guild_id, "guild_id")
    clean_source = str(source_name or "").strip()
    clean_fingerprint = str(source_fingerprint or "").strip()
    if not clean_source or not clean_fingerprint:
        raise ValueError("source_name and source_fingerprint must not be blank")
    import_id = _derived_uuid(
        "sheet.import",
        guild_id,
        clean_source,
        clean_fingerprint,
    )
    now = _utc_now()
    metadata_json = _canonical_json(dict(metadata or {}))
    with _transaction(database_path) as connection:
        _require_active_ledger(connection, guild_id)
        existing = connection.execute(
            "SELECT * FROM sheet_imports WHERE import_id = ?",
            (import_id,),
        ).fetchone()
        if existing is not None:
            if existing["metadata_json"] != metadata_json:
                raise IdempotencyConflictError(
                    "sheet import fingerprint was reused with different metadata"
                )
            if existing["status"] == SheetImportStatus.FAILED.value:
                connection.execute(
                    """
                    UPDATE sheet_imports
                    SET status = 'running', completed_at = NULL, error = NULL
                    WHERE import_id = ?
                    """,
                    (import_id,),
                )
                existing = connection.execute(
                    "SELECT * FROM sheet_imports WHERE import_id = ?",
                    (import_id,),
                ).fetchone()
            return _row_to_sheet_import(existing)
        connection.execute(
            """
            INSERT INTO sheet_imports (
                import_id, guild_id, source_name, source_fingerprint, status,
                metadata_json, started_at
            ) VALUES (?, ?, ?, ?, 'running', ?, ?)
            """,
            (
                import_id,
                guild_id,
                clean_source,
                clean_fingerprint,
                metadata_json,
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM sheet_imports WHERE import_id = ?",
            (import_id,),
        ).fetchone()
        return _row_to_sheet_import(row)


def record_sheet_import_row(
    import_id: str,
    worksheet_kind: str,
    row_number: int,
    row_fingerprint: str,
    payload: Mapping[str, Any] | Sequence[Any],
    *,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    database_path: DatabasePath = None,
) -> bool:
    clean_import_id = str(import_id or "").strip()
    clean_kind = str(worksheet_kind or "").strip()
    clean_fingerprint = str(row_fingerprint or "").strip()
    if not clean_import_id or not clean_kind or not clean_fingerprint:
        raise ValueError("import_id, worksheet_kind, and row_fingerprint must not be blank")
    if isinstance(row_number, bool) or not isinstance(row_number, int) or row_number <= 0:
        raise ValueError("row_number must be a positive integer")
    payload_json = _canonical_json(payload)
    now = _utc_now()
    with _transaction(database_path) as connection:
        sheet_import = connection.execute(
            "SELECT status FROM sheet_imports WHERE import_id = ?",
            (clean_import_id,),
        ).fetchone()
        if sheet_import is None:
            raise RepositoryError(f"sheet import {clean_import_id} does not exist")
        existing = connection.execute(
            """
            SELECT * FROM sheet_import_rows
            WHERE import_id = ? AND worksheet_kind = ? AND row_number = ?
            """,
            (clean_import_id, clean_kind, row_number),
        ).fetchone()
        if existing is not None:
            if (
                existing["row_fingerprint"] != clean_fingerprint
                or existing["payload_json"] != payload_json
            ):
                raise IdempotencyConflictError(
                    "an imported Sheet row changed within the same source snapshot"
                )
            return False
        if sheet_import["status"] == SheetImportStatus.COMPLETED.value:
            raise RepositoryError("cannot append rows to a completed Sheet import")
        connection.execute(
            """
            INSERT INTO sheet_import_rows (
                import_id, worksheet_kind, row_number, row_fingerprint,
                payload_json, entity_type, entity_id, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                clean_import_id,
                clean_kind,
                row_number,
                clean_fingerprint,
                payload_json,
                _clean_optional_string(entity_type),
                _clean_optional_string(entity_id),
                now,
            ),
        )
        return True


def complete_sheet_import(
    import_id: str,
    *,
    snapshot_id: Optional[str] = None,
    database_path: DatabasePath = None,
) -> Optional[SheetImport]:
    clean_import_id = str(import_id or "").strip()
    if not clean_import_id:
        raise ValueError("import_id must not be blank")
    now = _utc_now()
    with _transaction(database_path) as connection:
        import_row = connection.execute(
            "SELECT * FROM sheet_imports WHERE import_id = ?",
            (clean_import_id,),
        ).fetchone()
        if import_row is None:
            return None
        clean_snapshot_id = _clean_optional_string(snapshot_id)
        if clean_snapshot_id is not None:
            snapshot_row = connection.execute(
                "SELECT * FROM sheet_import_snapshots WHERE snapshot_id = ?",
                (clean_snapshot_id,),
            ).fetchone()
            if snapshot_row is None:
                raise RepositoryError(f"sheet snapshot {clean_snapshot_id} does not exist")
            if (
                int(snapshot_row["guild_id"]) != int(import_row["guild_id"])
                or str(snapshot_row["source_name"]) != str(import_row["source_name"])
                or str(snapshot_row["source_fingerprint"]) != str(import_row["source_fingerprint"])
            ):
                raise IdempotencyConflictError(
                    "sheet import does not belong to the staged snapshot"
                )
        row_count = connection.execute(
            "SELECT COUNT(*) FROM sheet_import_rows WHERE import_id = ?",
            (clean_import_id,),
        ).fetchone()[0]
        connection.execute(
            """
            UPDATE sheet_imports
            SET status = 'completed', completed_at = ?, row_count = ?, error = NULL
            WHERE import_id = ?
            """,
            (now, row_count, clean_import_id),
        )
        if clean_snapshot_id is not None:
            connection.execute(
                """
                UPDATE sheet_import_snapshots
                SET status = 'applied', applied_at = ?
                WHERE snapshot_id = ?
                """,
                (now, clean_snapshot_id),
            )
        row = connection.execute(
            "SELECT * FROM sheet_imports WHERE import_id = ?",
            (clean_import_id,),
        ).fetchone()
        return None if row is None else _row_to_sheet_import(row)


def fail_sheet_import(
    import_id: str,
    error: str,
    *,
    database_path: DatabasePath = None,
) -> Optional[SheetImport]:
    clean_import_id = str(import_id or "").strip()
    if not clean_import_id:
        raise ValueError("import_id must not be blank")
    now = _utc_now()
    with _transaction(database_path) as connection:
        row_count = connection.execute(
            "SELECT COUNT(*) FROM sheet_import_rows WHERE import_id = ?",
            (clean_import_id,),
        ).fetchone()[0]
        connection.execute(
            """
            UPDATE sheet_imports
            SET status = 'failed', completed_at = ?, row_count = ?, error = ?
            WHERE import_id = ?
            """,
            (now, row_count, str(error or "")[:2000], clean_import_id),
        )
        row = connection.execute(
            "SELECT * FROM sheet_imports WHERE import_id = ?",
            (clean_import_id,),
        ).fetchone()
        return None if row is None else _row_to_sheet_import(row)


def _row_to_issue(row: sqlite3.Row) -> MigrationIssue:
    return MigrationIssue(
        issue_id=str(row["issue_id"]),
        guild_id=None if row["guild_id"] is None else int(row["guild_id"]),
        source=str(row["source"]),
        source_reference=str(row["source_reference"]),
        code=str(row["code"]),
        message=str(row["message"]),
        payload=_decode_mapping_json(
            row["payload_json"],
            location=f"migration_issues/{row['issue_id']}",
        ),
        created_at=str(row["created_at"]),
        resolved_at=row["resolved_at"],
    )


def record_migration_issue(
    *,
    guild_id: Optional[int],
    source: str,
    source_reference: str,
    code: str,
    message: str,
    payload: Optional[Mapping[str, Any]] = None,
    deduplication_key: Optional[str] = None,
    database_path: DatabasePath = None,
) -> MigrationIssue:
    if guild_id is not None:
        _validate_identifier(guild_id, "guild_id")
    clean_source = str(source or "").strip()
    clean_reference = str(source_reference or "").strip()
    clean_code = str(code or "").strip()
    clean_message = str(message or "").strip()
    if not all((clean_source, clean_reference, clean_code, clean_message)):
        raise ValueError("migration issue fields must not be blank")
    payload_value = dict(payload or {})
    payload_json = _canonical_json(payload_value)
    issue_key = deduplication_key or _canonical_json(
        {
            "guild_id": guild_id,
            "source": clean_source,
            "source_reference": clean_reference,
            "code": clean_code,
            "payload": payload_value,
        }
    )
    issue_id = _derived_uuid(
        "migration.issue",
        guild_id if guild_id is not None else "global",
        clean_source,
        clean_code,
        issue_key,
    )
    now = _utc_now()
    with _transaction(database_path) as connection:
        connection.execute(
            """
            INSERT INTO migration_issues (
                issue_id, guild_id, source, source_reference, code, message,
                payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (issue_id) DO NOTHING
            """,
            (
                issue_id,
                guild_id,
                clean_source,
                clean_reference,
                clean_code,
                clean_message,
                payload_json,
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM migration_issues WHERE issue_id = ?",
            (issue_id,),
        ).fetchone()
        return _row_to_issue(row)


def list_migration_issues(
    *,
    guild_id: Optional[int] = None,
    open_only: bool = True,
    database_path: DatabasePath = None,
) -> list[MigrationIssue]:
    if guild_id is not None:
        _validate_identifier(guild_id, "guild_id")
    clauses: list[str] = []
    parameters: list[Any] = []
    if guild_id is not None:
        clauses.append("guild_id = ?")
        parameters.append(guild_id)
    if open_only:
        clauses.append("resolved_at IS NULL")
    where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _connection(database_path) as connection:
        rows = connection.execute(
            f"""
            SELECT * FROM migration_issues
            {where_clause}
            ORDER BY created_at, issue_id
            """,
            parameters,
        ).fetchall()
        return [_row_to_issue(row) for row in rows]


def resolve_migration_issue(
    issue_id: str,
    *,
    database_path: DatabasePath = None,
) -> bool:
    clean_issue_id = str(issue_id or "").strip()
    if not clean_issue_id:
        raise ValueError("issue_id must not be blank")
    with _transaction(database_path) as connection:
        cursor = connection.execute(
            """
            UPDATE migration_issues
            SET resolved_at = ?
            WHERE issue_id = ? AND resolved_at IS NULL
            """,
            (_utc_now(), clean_issue_id),
        )
        return cursor.rowcount == 1


def iter_balance_history(
    guild_id: int,
    *,
    discord_user_id: Optional[int] = None,
    database_path: DatabasePath = None,
) -> Iterable[sqlite3.Row]:
    """Return immutable audit rows as detached ``sqlite3.Row`` objects."""

    _validate_identifier(guild_id, "guild_id")
    if discord_user_id is not None:
        _validate_identifier(discord_user_id, "discord_user_id")
    player_clause = "AND discord_user_id = ?" if discord_user_id is not None else ""
    parameters: list[Any] = [guild_id]
    if discord_user_id is not None:
        parameters.append(discord_user_id)
    with _connection(database_path) as connection:
        return list(
            connection.execute(
                f"""
                SELECT * FROM balance_history
                WHERE guild_id = ? {player_clause}
                ORDER BY occurred_at, created_at, event_id
                """,
                parameters,
            ).fetchall()
        )


def iter_lootsplit_history(
    guild_id: int,
    *,
    database_path: DatabasePath = None,
) -> Iterable[Mapping[str, Any]]:
    """Return native and imported lootsplit audit rows with stable event IDs."""

    _validate_identifier(guild_id, "guild_id")
    with _connection(database_path) as connection:
        native_rows = connection.execute(
            """
            SELECT
                participant.history_event_id AS event_id,
                'lootsplit' AS event_kind,
                split.battleboard_ids_json AS battleboard_ids_json,
                split.occurred_at AS occurred_at,
                split.actor_name AS actor_name,
                split.officer_name AS officer_name,
                split.content_name AS content_name,
                split.caller_name AS caller_name,
                participant.nickname_snapshot AS nickname,
                participant.amount AS amount,
                split.created_at AS created_at
            FROM lootsplit_participants AS participant
            JOIN lootsplits AS split
                ON split.lootsplit_id = participant.lootsplit_id
            WHERE split.guild_id = ?
            """,
            (guild_id,),
        ).fetchall()
        imported_rows = connection.execute(
            """
            SELECT
                event_id,
                'sheet_import' AS event_kind,
                battleboard_ids_json,
                occurred_at,
                actor_name,
                actor_name AS officer_name,
                content_name,
                caller_name,
                nickname_snapshot AS nickname,
                amount,
                created_at
            FROM imported_lootsplit_history
            WHERE guild_id = ?
            """,
            (guild_id,),
        ).fetchall()
    result = [
        {
            "event_id": str(row["event_id"]),
            "event_kind": str(row["event_kind"]),
            "battleboard_ids": tuple(
                _decode_list_json(
                    row["battleboard_ids_json"],
                    location=f"lootsplit_history/{row['event_id']}/battleboards",
                )
            ),
            "occurred_at": str(row["occurred_at"]),
            "actor_name": str(row["actor_name"]),
            "officer_name": str(row["officer_name"] or row["actor_name"]),
            "content_name": str(row["content_name"]),
            "caller_name": str(row["caller_name"]),
            "nickname": str(row["nickname"]),
            "amount": int(row["amount"]),
            "created_at": str(row["created_at"]),
        }
        for row in (*native_rows, *imported_rows)
    ]
    return sorted(
        result,
        key=lambda row: (row["occurred_at"], row["created_at"], row["event_id"]),
    )


__all__ = [
    "SQLITE_INTEGER_MAX",
    "SQLITE_INTEGER_MIN",
    "BalanceChangeResult",
    "BalanceSnapshot",
    "DeadLetterEvent",
    "HistoryImportResult",
    "HistoryImportStatus",
    "IdempotencyConflictError",
    "LedgerGeneration",
    "LedgerNotActiveError",
    "LootsplitCredit",
    "LootsplitResult",
    "MigrationIssue",
    "OutboxEvent",
    "OutboxStatus",
    "PlayerImportResult",
    "PlayerImportStatus",
    "PlayerRecord",
    "RegistrationResult",
    "RegistrationStatus",
    "RepositoryCorruptionError",
    "RepositoryError",
    "SheetImport",
    "SheetImportSnapshot",
    "SheetImportStatus",
    "SheetSnapshotStatus",
    "SilverLeaderboardPage",
    "SiphonCacheResult",
    "SiphonCacheStatus",
    "SiphonUpdate",
    "TargetGuildConflictError",
    "ack_outbox",
    "activate_ledger",
    "activate_ledger_in_transaction",
    "apply_lootsplit",
    "archive_active_ledger_in_transaction",
    "begin_sheet_import",
    "cache_siphon",
    "cache_siphons",
    "change_balance",
    "claim_pending_outbox",
    "complete_sheet_import",
    "dead_letter_outbox",
    "ensure_schema",
    "fail_outbox",
    "fail_sheet_import",
    "get_active_ledger",
    "get_active_ledger_id",
    "get_balance_snapshot",
    "get_latest_completed_sheet_import",
    "get_outbox_status",
    "get_player",
    "get_player_by_nickname",
    "get_silver_leaderboard",
    "get_silver_leaderboard_position",
    "get_staged_sheet_snapshot",
    "has_completed_sheet_import",
    "has_dead_letter_outbox",
    "has_incomplete_outbox",
    "import_balance_history",
    "import_lootsplit_history",
    "import_player",
    "iter_balance_history",
    "iter_lootsplit_history",
    "list_active_players",
    "list_dead_letter_outbox",
    "list_ledger_generations",
    "list_migration_issues",
    "list_negative_siphon",
    "list_pending_outbox",
    "list_players",
    "normalize_nickname",
    "prune_applied_sheet_snapshots",
    "prune_completed_outbox",
    "record_migration_issue",
    "record_sheet_import_row",
    "register_player",
    "resolve_migration_issue",
    "retry_dead_letter_outbox",
    "retry_dead_letter_outbox_for_guild",
    "set_in_guild",
    "stage_sheet_snapshot",
]
