"""Idempotent import of pre-SQLite Realm Protector configuration files."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional, Union

from src.realm_protector.infrastructure import sqlite_database

PathLike = Union[str, Path]


@dataclass(frozen=True)
class LegacySourceResult:
    source_key: str
    source_path: str
    status: str
    discovered_records: int = 0
    imported_records: int = 0
    message: str = ""

    @property
    def failed(self) -> bool:
        return self.status == "failed"


@dataclass(frozen=True)
class LegacyMigrationReport:
    database_path: str
    sources: tuple[LegacySourceResult, ...]

    @property
    def failed(self) -> bool:
        return any(source.failed for source in self.sources)

    @property
    def imported_records(self) -> int:
        return sum(source.imported_records for source in self.sources)

    def to_dict(self) -> dict[str, Any]:
        return {
            "database_path": self.database_path,
            "failed": self.failed,
            "imported_records": self.imported_records,
            "sources": [asdict(source) for source in self.sources],
        }


_CONFIGURATION_SOURCES: tuple[tuple[str, str, Path], ...] = (
    (
        "legacy-json:guild-settings",
        "guild_settings",
        Path("configs/guilds_config.json"),
    ),
    (
        "legacy-json:tickets",
        "tickets",
        Path("configs/tickets_config.json"),
    ),
    (
        "legacy-json:reaction-roles",
        "reaction_roles",
        Path("configs/role_reaction_config.json"),
    ),
    (
        "legacy-json:objectives",
        "objectives",
        Path("configs/objectives_config.json"),
    ),
)
_GOOGLE_LINKS_SOURCE_KEY = "legacy-json:google-sheet-links"
_GOOGLE_LINKS_PATH = Path("google_sheet_credentials/credentials_links.json")


def _read_legacy_mapping(
    source_key: str,
    source_path: Path,
) -> tuple[Optional[dict], Optional[str], LegacySourceResult]:
    display_path = str(source_path)
    if not source_path.exists():
        return (
            None,
            None,
            LegacySourceResult(source_key, display_path, "missing"),
        )
    if source_path.is_symlink() or not source_path.is_file():
        return (
            None,
            None,
            LegacySourceResult(
                source_key,
                display_path,
                "failed",
                message="Legacy source must be a regular file, not a symbolic link.",
            ),
        )

    try:
        source_bytes = source_path.read_bytes()
        document = json.loads(source_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return (
            None,
            None,
            LegacySourceResult(
                source_key,
                display_path,
                "failed",
                message=f"Legacy JSON could not be read: {type(error).__name__}.",
            ),
        )

    if not isinstance(document, dict):
        return (
            None,
            None,
            LegacySourceResult(
                source_key,
                display_path,
                "failed",
                message="Legacy JSON root must be an object.",
            ),
        )

    fingerprint = hashlib.sha256(source_bytes).hexdigest()
    return (
        document,
        fingerprint,
        LegacySourceResult(
            source_key,
            display_path,
            "ready",
            discovered_records=len(document),
        ),
    )


def _payload_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def _record_source_import(
    database: sqlite3.Connection,
    *,
    source_key: str,
    source_path: Path,
    fingerprint: str,
    discovered_records: int,
    imported_records: int,
) -> None:
    notes_json = _payload_json(
        {
            "discovered_records": discovered_records,
            "local_records_preserved": discovered_records - imported_records,
        }
    )
    database.execute(
        """
        INSERT INTO legacy_imports (
            source_key,
            source_path,
            source_fingerprint,
            last_seen_fingerprint,
            imported_records,
            notes_json,
            imported_at,
            last_checked_at
        ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT (source_key) DO UPDATE SET
            source_path = excluded.source_path,
            last_seen_fingerprint = excluded.last_seen_fingerprint,
            last_checked_at = CURRENT_TIMESTAMP
        """,
        (
            source_key,
            str(source_path),
            fingerprint,
            fingerprint,
            imported_records,
            notes_json,
        ),
    )


def _already_imported_result(
    database: sqlite3.Connection,
    *,
    previous: sqlite3.Row,
    source_key: str,
    source_path: Path,
    fingerprint: str,
    discovered_records: int,
) -> LegacySourceResult:
    fingerprint_changed = previous["source_fingerprint"] != fingerprint
    try:
        notes = json.loads(previous["notes_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        notes = {}
    if not isinstance(notes, dict):
        notes = {}
    notes["last_observation"] = {
        "discovered_records": discovered_records,
        "fingerprint_changed": fingerprint_changed,
        "change_ignored": fingerprint_changed,
    }
    database.execute(
        """
        UPDATE legacy_imports
        SET source_path = ?,
            last_seen_fingerprint = ?,
            last_checked_at = CURRENT_TIMESTAMP,
            notes_json = ?
        WHERE source_key = ?
        """,
        (str(source_path), fingerprint, _payload_json(notes), source_key),
    )
    message = (
        "Legacy source changed after migration; the change was ignored because "
        "SQLite is authoritative."
        if fingerprint_changed
        else ""
    )
    return LegacySourceResult(
        source_key,
        str(source_path),
        "already_imported",
        discovered_records=discovered_records,
        message=message,
    )


def _observe_completed_source(
    source_key: str,
    source_path: Path,
    *,
    database_path: Optional[PathLike],
) -> Optional[LegacySourceResult]:
    """Inspect a stale backup only after proving its import already completed.

    SQLite remains usable even if an old JSON backup is later deleted, malformed,
    replaced by a symlink, or made unreadable. A readable change is still
    fingerprinted and recorded as ignored for operator visibility.
    """

    with sqlite_database.transaction(database_path) as database:
        previous = database.execute(
            """
            SELECT source_fingerprint, notes_json
            FROM legacy_imports
            WHERE source_key = ?
            """,
            (source_key,),
        ).fetchone()
        if previous is None:
            return None

        fingerprint = str(previous["source_fingerprint"])
        discovered_records = 0
        observation = ""
        try:
            if source_path.is_symlink() or not source_path.is_file():
                observation = (
                    "Legacy backup is unavailable or unsafe; SQLite remains authoritative."
                )
            else:
                source_bytes = source_path.read_bytes()
                fingerprint = hashlib.sha256(source_bytes).hexdigest()
                try:
                    parsed = json.loads(source_bytes.decode("utf-8"))
                    if isinstance(parsed, dict):
                        discovered_records = len(parsed)
                    else:
                        observation = (
                            "Legacy backup no longer contains an object; the change was ignored."
                        )
                except (UnicodeDecodeError, json.JSONDecodeError):
                    observation = "Legacy backup is malformed; the change was ignored."
        except OSError:
            observation = "Legacy backup could not be read; SQLite remains authoritative."

        result = _already_imported_result(
            database,
            previous=previous,
            source_key=source_key,
            source_path=source_path,
            fingerprint=fingerprint,
            discovered_records=discovered_records,
        )
        if not observation:
            return result
        message = " ".join(value for value in (result.message, observation) if value)
        return LegacySourceResult(
            result.source_key,
            result.source_path,
            result.status,
            discovered_records=result.discovered_records,
            imported_records=result.imported_records,
            message=message,
        )


def _import_configuration_source(
    source_key: str,
    namespace: str,
    source_path: Path,
    *,
    database_path: Optional[PathLike],
) -> LegacySourceResult:
    completed = _observe_completed_source(
        source_key,
        source_path,
        database_path=database_path,
    )
    if completed is not None:
        return completed
    document, fingerprint, initial_result = _read_legacy_mapping(
        source_key,
        source_path,
    )
    if document is None or fingerprint is None:
        return initial_result

    try:
        with sqlite_database.transaction(database_path) as database:
            previous = database.execute(
                """
                SELECT source_fingerprint, notes_json
                FROM legacy_imports
                WHERE source_key = ?
                """,
                (source_key,),
            ).fetchone()
            if previous:
                return _already_imported_result(
                    database,
                    previous=previous,
                    source_key=source_key,
                    source_path=source_path,
                    fingerprint=fingerprint,
                    discovered_records=len(document),
                )

            imported_records = 0
            for guild_id, payload in document.items():
                cursor = database.execute(
                    """
                    INSERT INTO configuration_documents (
                        namespace,
                        guild_id,
                        payload_json,
                        updated_at
                    ) VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT (namespace, guild_id) DO NOTHING
                    """,
                    (namespace, str(guild_id), _payload_json(payload)),
                )
                imported_records += max(cursor.rowcount, 0)

            _record_source_import(
                database,
                source_key=source_key,
                source_path=source_path,
                fingerprint=fingerprint,
                discovered_records=len(document),
                imported_records=imported_records,
            )
    except (OSError, sqlite3.Error, TypeError, ValueError) as error:
        return LegacySourceResult(
            source_key,
            str(source_path),
            "failed",
            discovered_records=len(document),
            message=f"SQLite import failed: {type(error).__name__}.",
        )

    return LegacySourceResult(
        source_key,
        str(source_path),
        "imported",
        discovered_records=len(document),
        imported_records=imported_records,
    )


def _import_google_links(
    source_path: Path,
    *,
    database_path: Optional[PathLike],
) -> LegacySourceResult:
    source_key = _GOOGLE_LINKS_SOURCE_KEY
    completed = _observe_completed_source(
        source_key,
        source_path,
        database_path=database_path,
    )
    if completed is not None:
        return completed
    document, fingerprint, initial_result = _read_legacy_mapping(
        source_key,
        source_path,
    )
    if document is None or fingerprint is None:
        return initial_result

    try:
        with sqlite_database.transaction(database_path) as database:
            previous = database.execute(
                """
                SELECT source_fingerprint, notes_json
                FROM legacy_imports
                WHERE source_key = ?
                """,
                (source_key,),
            ).fetchone()
            if previous:
                return _already_imported_result(
                    database,
                    previous=previous,
                    source_key=source_key,
                    source_path=source_path,
                    fingerprint=fingerprint,
                    discovered_records=len(document),
                )

            imported_records = 0
            for guild_id, payload in document.items():
                cursor = database.execute(
                    """
                    INSERT INTO google_sheet_links (
                        guild_id,
                        payload_json,
                        updated_at
                    ) VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT (guild_id) DO NOTHING
                    """,
                    (str(guild_id), _payload_json(payload)),
                )
                imported_records += max(cursor.rowcount, 0)

            _record_source_import(
                database,
                source_key=source_key,
                source_path=source_path,
                fingerprint=fingerprint,
                discovered_records=len(document),
                imported_records=imported_records,
            )
    except (OSError, sqlite3.Error, TypeError, ValueError) as error:
        return LegacySourceResult(
            source_key,
            str(source_path),
            "failed",
            discovered_records=len(document),
            message=f"SQLite import failed: {type(error).__name__}.",
        )

    return LegacySourceResult(
        source_key,
        str(source_path),
        "imported",
        discovered_records=len(document),
        imported_records=imported_records,
    )


def migrate_legacy_storage(
    project_root: PathLike = Path("."),
    *,
    database_path: Optional[PathLike] = None,
) -> LegacyMigrationReport:
    """Import all known legacy JSON files without modifying or deleting them.

    A SHA-256 fingerprint records the imported source. Once a source has been
    imported, later file changes are recorded as ignored and never applied, so
    deleted or edited local state cannot be resurrected by stale JSON.
    """

    root = Path(project_root).expanduser()
    active_database_path = sqlite_database.initialize_database(database_path)
    results = [
        _import_configuration_source(
            source_key,
            namespace,
            root / relative_path,
            database_path=active_database_path,
        )
        for source_key, namespace, relative_path in _CONFIGURATION_SOURCES
    ]
    results.append(
        _import_google_links(
            root / _GOOGLE_LINKS_PATH,
            database_path=active_database_path,
        )
    )
    return LegacyMigrationReport(str(active_database_path), tuple(results))
