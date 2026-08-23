"""SQLite connection management and core persistence schema.

The module deliberately owns only cross-cutting storage tables. Feature-specific
repositories may add their own tables with idempotent ``CREATE TABLE``
statements while sharing the connection and transaction helpers below.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Union

PathLike = Union[str, os.PathLike[str]]
DEFAULT_DATABASE_PATH = Path("data/realm_protector.sqlite3")
DATABASE_PATH_ENVIRONMENT_VARIABLE = "REALM_PROTECTOR_DATABASE_PATH"
BUSY_TIMEOUT_MILLISECONDS = 5_000

_DATABASE_PATH_LOCK = threading.RLock()
_INITIALIZATION_LOCK = threading.RLock()
_configured_database_path: Optional[Path] = None
_initialized_database_files: dict[Path, tuple[int, int]] = {}


_SCHEMA_MIGRATIONS: tuple[tuple[int, str, tuple[str, ...]], ...] = (
    (
        1,
        "core persistence tables",
        (
            """
            CREATE TABLE IF NOT EXISTS legacy_imports (
                source_key TEXT PRIMARY KEY,
                source_path TEXT NOT NULL,
                source_fingerprint TEXT NOT NULL,
                imported_records INTEGER NOT NULL DEFAULT 0,
                notes_json TEXT NOT NULL DEFAULT '{}',
                imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS configuration_documents (
                namespace TEXT NOT NULL,
                guild_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (namespace, guild_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS google_sheet_links (
                guild_id TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS runtime_records (
                kind TEXT NOT NULL,
                guild_id INTEGER NOT NULL,
                external_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (kind, guild_id, external_id)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_runtime_records_status
            ON runtime_records (kind, status, guild_id)
            """,
        ),
    ),
    (
        2,
        "track ignored legacy source changes",
        (
            """
            ALTER TABLE legacy_imports
            ADD COLUMN last_seen_fingerprint TEXT
            """,
            """
            ALTER TABLE legacy_imports
            ADD COLUMN last_checked_at TEXT
            """,
            """
            UPDATE legacy_imports
            SET last_seen_fingerprint = source_fingerprint,
                last_checked_at = imported_at
            WHERE last_seen_fingerprint IS NULL
            """,
        ),
    ),
)


def get_database_path() -> Path:
    """Return the configured database path without opening the database."""

    with _DATABASE_PATH_LOCK:
        if _configured_database_path is not None:
            return _configured_database_path

    environment_path = os.environ.get(
        DATABASE_PATH_ENVIRONMENT_VARIABLE,
        "",
    ).strip()
    return Path(environment_path).expanduser() if environment_path else DEFAULT_DATABASE_PATH


def configure_database(path: Optional[PathLike]) -> Path:
    """Select a database file for subsequent connections.

    Passing ``None`` clears the process override and restores the environment or
    default path. Existing connections are intentionally unaffected.
    """

    global _configured_database_path
    with _DATABASE_PATH_LOCK:
        _configured_database_path = Path(path).expanduser() if path is not None else None
    return get_database_path()


def resolve_project_database_path(project_root: PathLike, path: PathLike) -> Path:
    """Resolve a configured path, confining relative values to the project root."""

    root = Path(project_root).expanduser().resolve()
    configured = Path(path).expanduser()
    if configured.is_absolute():
        return configured
    resolved = (root / configured).resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("A relative SQLite path must remain inside the project root.")
    return resolved


@contextmanager
def database_path(path: PathLike) -> Iterator[Path]:
    """Temporarily configure a database path, primarily for isolated tests."""

    global _configured_database_path
    with _DATABASE_PATH_LOCK:
        previous_path = _configured_database_path
        _configured_database_path = Path(path).expanduser()
    try:
        yield get_database_path()
    finally:
        with _DATABASE_PATH_LOCK:
            _configured_database_path = previous_path


def _prepare_database_location(path: Path) -> None:
    if path.is_symlink():
        raise OSError("Refusing to use a symbolic link as the SQLite database.")

    parent = path.parent
    if parent.is_symlink():
        raise OSError("Refusing to use a symbolic link as the SQLite data directory.")
    parent_existed = parent.exists()
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not parent_existed or (
        parent.name == DEFAULT_DATABASE_PATH.parent.name and path.name == DEFAULT_DATABASE_PATH.name
    ):
        os.chmod(parent, 0o700)


def _open_connection(path: Path) -> sqlite3.Connection:
    _prepare_database_location(path)
    database = sqlite3.connect(
        str(path),
        timeout=BUSY_TIMEOUT_MILLISECONDS / 1_000,
        isolation_level=None,
    )
    database.row_factory = sqlite3.Row
    try:
        database.execute("PRAGMA foreign_keys = ON")
        database.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MILLISECONDS}")
        database.execute("PRAGMA journal_mode = WAL")
        # This file is the authoritative economy ledger.  FULL keeps an
        # acknowledged commit durable across an operating-system or power loss,
        # not merely across an application crash.  The bot's write volume is low
        # enough that the additional WAL sync is the safer default.
        database.execute("PRAGMA synchronous = FULL")
        _harden_database_permissions(path)
    except Exception:
        database.close()
        raise
    return database


def _harden_database_permissions(path: Path) -> None:
    if path.exists() and not path.is_symlink():
        os.chmod(path, 0o600)
    for suffix in ("-wal", "-shm"):
        companion = Path(f"{path}{suffix}")
        if companion.exists() and not companion.is_symlink():
            os.chmod(companion, 0o600)


def initialize_database(path: Optional[PathLike] = None) -> Path:
    """Create or upgrade the core schema and return the active file path."""

    resolved_path = Path(path).expanduser() if path is not None else get_database_path()
    cache_key = resolved_path.absolute()
    with _INITIALIZATION_LOCK:
        try:
            database_stat = resolved_path.stat()
            identity = (database_stat.st_dev, database_stat.st_ino)
        except OSError:
            identity = None
        if identity is not None and _initialized_database_files.get(cache_key) == identity:
            return resolved_path

        database = _open_connection(resolved_path)
        try:
            database.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            applied_versions = {
                int(row["version"])
                for row in database.execute("SELECT version FROM schema_migrations").fetchall()
            }
            for version, name, statements in _SCHEMA_MIGRATIONS:
                if version in applied_versions:
                    continue
                database.execute("BEGIN IMMEDIATE")
                try:
                    for statement in statements:
                        database.execute(statement)
                    database.execute(
                        "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                        (version, name),
                    )
                    database.execute(f"PRAGMA user_version = {version}")
                    database.execute("COMMIT")
                except Exception:
                    database.execute("ROLLBACK")
                    raise
        finally:
            database.close()
        _harden_database_permissions(resolved_path)
        database_stat = resolved_path.stat()
        _initialized_database_files[cache_key] = (
            database_stat.st_dev,
            database_stat.st_ino,
        )
    return resolved_path


def connect(path: Optional[PathLike] = None) -> sqlite3.Connection:
    """Open a configured SQLite connection; the caller must close it."""

    resolved_path = initialize_database(path)
    return _open_connection(resolved_path)


@contextmanager
def connection(path: Optional[PathLike] = None) -> Iterator[sqlite3.Connection]:
    """Yield a configured connection and always close it."""

    resolved_path = Path(path).expanduser() if path is not None else get_database_path()
    database = connect(resolved_path)
    try:
        yield database
    finally:
        database.close()
        # Use the path captured when the connection opened.  A concurrent test
        # or administrative reconfiguration must not make us chmod another DB.
        _harden_database_permissions(resolved_path)


@contextmanager
def transaction(
    path: Optional[PathLike] = None,
    *,
    immediate: bool = True,
) -> Iterator[sqlite3.Connection]:
    """Yield a connection inside a commit-or-rollback transaction."""

    with connection(path) as database:
        database.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield database
        except Exception:
            database.execute("ROLLBACK")
            raise
        else:
            database.execute("COMMIT")
