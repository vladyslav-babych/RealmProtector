"""SQLite-backed adapters for schemaless guild configuration documents."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Union

from src.realm_protector.infrastructure import sqlite_database

PathLike = Union[str, Path]


class DocumentCorruptionError(RuntimeError):
    """Raised when a persisted configuration row is not valid JSON."""


def _validated_namespace(namespace: str) -> str:
    clean_namespace = str(namespace or "").strip()
    if not clean_namespace:
        raise ValueError("Document namespace cannot be empty.")
    return clean_namespace


def _encode_payload(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _decode_payload(payload_json: str, *, location: str = "configuration") -> Any:
    try:
        return json.loads(payload_json)
    except (json.JSONDecodeError, TypeError) as error:
        raise DocumentCorruptionError(f"Persisted {location} payload is corrupt.") from error


@contextmanager
def _read_connection(
    database_path: Optional[PathLike],
    database: Optional[sqlite3.Connection],
) -> Iterator[sqlite3.Connection]:
    if database is not None:
        yield database
        return
    with sqlite_database.connection(database_path) as opened:
        yield opened


@contextmanager
def _write_connection(
    database_path: Optional[PathLike],
    database: Optional[sqlite3.Connection],
) -> Iterator[sqlite3.Connection]:
    if database is not None:
        yield database
        return
    with sqlite_database.transaction(database_path) as opened:
        yield opened


def get_mapping_entry(
    namespace: str,
    guild_id: str | int,
    *,
    database_path: Optional[PathLike] = None,
    database: Optional[sqlite3.Connection] = None,
) -> Optional[Any]:
    """Load one configuration row without conflating absence and corruption."""

    clean_namespace = _validated_namespace(namespace)
    clean_guild_id = str(guild_id).strip()
    if not clean_guild_id:
        raise ValueError("Guild ID cannot be empty.")
    with _read_connection(database_path, database) as connection:
        row = connection.execute(
            """
            SELECT payload_json FROM configuration_documents
            WHERE namespace = ? AND guild_id = ?
            """,
            (clean_namespace, clean_guild_id),
        ).fetchone()
    if row is None:
        return None
    return _decode_payload(
        row["payload_json"],
        location=f"{clean_namespace}/{clean_guild_id}",
    )


def upsert_mapping_entry(
    namespace: str,
    guild_id: str | int,
    payload: Any,
    *,
    database_path: Optional[PathLike] = None,
    database: Optional[sqlite3.Connection] = None,
) -> Optional[Any]:
    """Upsert one row and return its previous value.

    Passing an existing ``database`` connection lets callers combine several
    namespaces and ledger changes in one outer SQLite transaction.
    """

    clean_namespace = _validated_namespace(namespace)
    clean_guild_id = str(guild_id).strip()
    if not clean_guild_id:
        raise ValueError("Guild ID cannot be empty.")
    encoded = _encode_payload(payload)
    with _write_connection(database_path, database) as connection:
        row = connection.execute(
            """
            SELECT payload_json FROM configuration_documents
            WHERE namespace = ? AND guild_id = ?
            """,
            (clean_namespace, clean_guild_id),
        ).fetchone()
        previous = (
            None
            if row is None
            else _decode_payload(
                row["payload_json"],
                location=f"{clean_namespace}/{clean_guild_id}",
            )
        )
        connection.execute(
            """
            INSERT INTO configuration_documents (
                namespace, guild_id, payload_json, updated_at
            ) VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT (namespace, guild_id) DO UPDATE SET
                payload_json = excluded.payload_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (clean_namespace, clean_guild_id, encoded),
        )
    return previous


def delete_mapping_entry(
    namespace: str,
    guild_id: str | int,
    *,
    database_path: Optional[PathLike] = None,
    database: Optional[sqlite3.Connection] = None,
) -> Optional[Any]:
    """Delete one row and return its previous value, if present."""

    clean_namespace = _validated_namespace(namespace)
    clean_guild_id = str(guild_id).strip()
    if not clean_guild_id:
        raise ValueError("Guild ID cannot be empty.")
    with _write_connection(database_path, database) as connection:
        row = connection.execute(
            """
            SELECT payload_json FROM configuration_documents
            WHERE namespace = ? AND guild_id = ?
            """,
            (clean_namespace, clean_guild_id),
        ).fetchone()
        if row is None:
            return None
        previous = _decode_payload(
            row["payload_json"],
            location=f"{clean_namespace}/{clean_guild_id}",
        )
        connection.execute(
            """
            DELETE FROM configuration_documents
            WHERE namespace = ? AND guild_id = ?
            """,
            (clean_namespace, clean_guild_id),
        )
    return previous


def load_mapping(
    namespace: str,
    *,
    database_path: Optional[PathLike] = None,
) -> dict[str, Any]:
    """Load every guild-keyed entry in a configuration namespace."""

    clean_namespace = _validated_namespace(namespace)
    with sqlite_database.connection(database_path) as database:
        rows = database.execute(
            """
            SELECT guild_id, payload_json
            FROM configuration_documents
            WHERE namespace = ?
            ORDER BY guild_id
            """,
            (clean_namespace,),
        ).fetchall()

    result: dict[str, Any] = {}
    for row in rows:
        result[str(row["guild_id"])] = _decode_payload(
            row["payload_json"],
            location=f"{clean_namespace}/{row['guild_id']}",
        )
    return result


def save_mapping(
    namespace: str,
    document: dict,
    *,
    database_path: Optional[PathLike] = None,
) -> None:
    """Atomically replace a complete configuration namespace."""

    if not isinstance(document, dict):
        raise TypeError("Configuration document must be a mapping.")
    clean_namespace = _validated_namespace(namespace)
    encoded_entries = [
        (clean_namespace, str(guild_id), _encode_payload(payload))
        for guild_id, payload in document.items()
    ]

    with sqlite_database.transaction(database_path) as database:
        database.execute(
            "DELETE FROM configuration_documents WHERE namespace = ?",
            (clean_namespace,),
        )
        database.executemany(
            """
            INSERT INTO configuration_documents (
                namespace,
                guild_id,
                payload_json,
                updated_at
            ) VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            """,
            encoded_entries,
        )


def load_google_sheet_links(
    *,
    database_path: Optional[PathLike] = None,
) -> dict[str, Any]:
    """Load Google Sheet link metadata; credential secrets remain on disk."""

    with sqlite_database.connection(database_path) as database:
        rows = database.execute(
            "SELECT guild_id, payload_json FROM google_sheet_links ORDER BY guild_id"
        ).fetchall()

    result: dict[str, Any] = {}
    for row in rows:
        result[str(row["guild_id"])] = _decode_payload(
            row["payload_json"],
            location=f"google_sheet_links/{row['guild_id']}",
        )
    return result


def get_google_sheet_link(
    guild_id: str | int,
    *,
    database_path: Optional[PathLike] = None,
    database: Optional[sqlite3.Connection] = None,
) -> Optional[Any]:
    """Load one guild's link, returning ``None`` only when no row exists.

    SQLite and JSON decoding failures intentionally propagate. A caller must
    never interpret an unavailable/corrupt database as an unlinked guild.
    """

    clean_guild_id = str(guild_id).strip()
    if not clean_guild_id:
        raise ValueError("Guild ID cannot be empty.")
    with _read_connection(database_path, database) as connection:
        row = connection.execute(
            "SELECT payload_json FROM google_sheet_links WHERE guild_id = ?",
            (clean_guild_id,),
        ).fetchone()
    if row is None:
        return None
    return _decode_payload(
        row["payload_json"],
        location=f"google_sheet_links/{clean_guild_id}",
    )


def upsert_google_sheet_link(
    guild_id: str | int,
    payload: Any,
    *,
    database_path: Optional[PathLike] = None,
    database: Optional[sqlite3.Connection] = None,
) -> Optional[Any]:
    """Insert or replace one guild link and return its previous payload.

    The read and write share one transaction, so updating one guild can never
    erase metadata belonging to another guild.
    """

    clean_guild_id = str(guild_id).strip()
    if not clean_guild_id:
        raise ValueError("Guild ID cannot be empty.")
    encoded_payload = _encode_payload(payload)
    with _write_connection(database_path, database) as connection:
        previous_row = connection.execute(
            "SELECT payload_json FROM google_sheet_links WHERE guild_id = ?",
            (clean_guild_id,),
        ).fetchone()
        previous_payload = (
            None
            if previous_row is None
            else _decode_payload(
                previous_row["payload_json"],
                location=f"google_sheet_links/{clean_guild_id}",
            )
        )
        connection.execute(
            """
            INSERT INTO google_sheet_links (guild_id, payload_json, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT (guild_id) DO UPDATE SET
                payload_json = excluded.payload_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (clean_guild_id, encoded_payload),
        )
    return previous_payload


def update_google_sheet_link_fields(
    guild_id: str | int,
    updates: Mapping[str, Any],
    *,
    database_path: Optional[PathLike] = None,
    database: Optional[sqlite3.Connection] = None,
) -> Optional[dict[str, Any]]:
    """Update fields in one link, returning ``None`` only when it is missing."""

    clean_guild_id = str(guild_id).strip()
    if not clean_guild_id:
        raise ValueError("Guild ID cannot be empty.")
    if not isinstance(updates, Mapping) or not updates:
        raise ValueError("Google Sheet link updates cannot be empty.")
    with _write_connection(database_path, database) as connection:
        row = connection.execute(
            "SELECT payload_json FROM google_sheet_links WHERE guild_id = ?",
            (clean_guild_id,),
        ).fetchone()
        if row is None:
            return None
        payload = _decode_payload(
            row["payload_json"],
            location=f"google_sheet_links/{clean_guild_id}",
        )
        if not isinstance(payload, dict):
            raise TypeError("Google Sheet link payload must be an object.")
        payload.update(dict(updates))
        connection.execute(
            """
            UPDATE google_sheet_links
            SET payload_json = ?, updated_at = CURRENT_TIMESTAMP
            WHERE guild_id = ?
            """,
            (_encode_payload(payload), clean_guild_id),
        )
    return payload


def delete_google_sheet_link(
    guild_id: str | int,
    *,
    database_path: Optional[PathLike] = None,
    database: Optional[sqlite3.Connection] = None,
) -> Optional[Any]:
    """Delete one guild link and return its payload, or ``None`` if absent."""

    clean_guild_id = str(guild_id).strip()
    if not clean_guild_id:
        raise ValueError("Guild ID cannot be empty.")
    with _write_connection(database_path, database) as connection:
        row = connection.execute(
            "SELECT payload_json FROM google_sheet_links WHERE guild_id = ?",
            (clean_guild_id,),
        ).fetchone()
        if row is None:
            return None
        payload = _decode_payload(
            row["payload_json"],
            location=f"google_sheet_links/{clean_guild_id}",
        )
        connection.execute(
            "DELETE FROM google_sheet_links WHERE guild_id = ?",
            (clean_guild_id,),
        )
    return payload


def is_google_credentials_file_referenced(
    credentials_file: str,
    *,
    database_path: Optional[PathLike] = None,
) -> bool:
    """Return whether any link references a credential file.

    Malformed rows and database failures propagate so callers fail safely and
    retain the secret instead of deleting a potentially shared credential.
    """

    clean_file = str(credentials_file or "").strip()
    if not clean_file:
        raise ValueError("Credentials file cannot be empty.")
    with sqlite_database.connection(database_path) as database:
        rows = database.execute("SELECT payload_json FROM google_sheet_links").fetchall()
    for row in rows:
        payload = _decode_payload(row["payload_json"])
        if isinstance(payload, dict) and payload.get("credentials_file") == clean_file:
            return True
    return False


def save_google_sheet_links(
    links: dict,
    *,
    database_path: Optional[PathLike] = None,
) -> None:
    """Upsert the supplied link rows without deleting other guild metadata.

    This compatibility bulk helper is intended for imports and tests. Runtime
    credential mutations use the single-row CRUD operations above.
    """

    if not isinstance(links, dict):
        raise TypeError("Google Sheet links must be a mapping.")
    encoded_entries = [
        (str(guild_id), _encode_payload(payload)) for guild_id, payload in links.items()
    ]
    with sqlite_database.transaction(database_path) as database:
        database.executemany(
            """
            INSERT INTO google_sheet_links (guild_id, payload_json, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT (guild_id) DO UPDATE SET
                payload_json = excluded.payload_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            encoded_entries,
        )
