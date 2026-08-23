import json
import os
import re
import sqlite3
import tempfile
import threading
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Callable, Optional, Tuple, TypeVar
from uuid import uuid4

from src.realm_protector.infrastructure import document_store

_CREDENTIALS_DIR = Path("google_sheet_credentials")
_LINKS_FILE = _CREDENTIALS_DIR / "credentials_links.json"
_LINKS_LOCK = threading.RLock()
_T = TypeVar("_T")


def _links_transaction(function: Callable[..., _T]) -> Callable[..., _T]:
    """Serialize credential-file and per-guild metadata operations in-process."""

    @wraps(function)
    def wrapped(*args, **kwargs):
        with _LINKS_LOCK:
            return function(*args, **kwargs)

    return wrapped


def _ensure_credentials_dir() -> None:
    if _CREDENTIALS_DIR.is_symlink():
        raise OSError("google_sheet_credentials must not be a symbolic link.")
    _CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(_CREDENTIALS_DIR, 0o700)


def harden_existing_credential_permissions() -> None:
    """Apply private modes to existing regular files without following symlinks."""

    _ensure_credentials_dir()
    for path in _CREDENTIALS_DIR.iterdir():
        if path.is_symlink() or not path.is_file():
            continue
        os.chmod(path, 0o600)


def _validated_credentials_file_name(file_name: object) -> str:
    clean_name = str(file_name or "").strip()
    candidate = Path(clean_name)
    if (
        not clean_name
        or candidate.is_absolute()
        or candidate.name != clean_name
        or clean_name in {".", ".."}
    ):
        raise ValueError("Credentials file must be a file name without directories.")
    return clean_name


def _credentials_path(file_name: object, *, require_existing: bool = False) -> Path:
    safe_name = _validated_credentials_file_name(file_name)
    _ensure_credentials_dir()
    credentials_root = _CREDENTIALS_DIR.resolve()
    candidate = credentials_root / safe_name

    if not require_existing:
        return candidate

    resolved = candidate.resolve(strict=True)
    if resolved.parent != credentials_root or not resolved.is_file():
        raise ValueError("Credentials file must be a regular file in google_sheet_credentials/.")
    return resolved


def _write_private_json(path: Path, document: dict) -> None:
    """Atomically write a private JSON document with owner-only permissions."""

    _ensure_credentials_dir()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".realm-protector-",
        suffix=".tmp",
        dir=_CREDENTIALS_DIR,
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            descriptor = -1
            json.dump(document, file, ensure_ascii=True, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
        directory_descriptor = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path.exists():
            temporary_path.unlink()


def _sanitize_guild_name(guild_name: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]", "_", guild_name.strip())
    sanitized = re.sub(r"_+", "_", sanitized).strip("_")
    return sanitized or "guild"


def _load_links() -> dict:
    data = document_store.load_google_sheet_links()
    return data if isinstance(data, dict) else {}


def _save_links(links: dict) -> None:
    """Compatibility fixture/import helper; runtime mutations are row-level."""

    try:
        document_store.save_google_sheet_links(links)
    except sqlite3.Error as error:
        raise OSError("Google Sheet link metadata could not be saved.") from error


def _get_link(discord_server_id: int):
    return document_store.get_google_sheet_link(discord_server_id)


def _upsert_link(discord_server_id: int, payload: dict, *, database=None):
    return document_store.upsert_google_sheet_link(
        discord_server_id,
        payload,
        database=database,
    )


def _update_link_fields(discord_server_id: int, updates: dict, *, database=None):
    return document_store.update_google_sheet_link_fields(
        discord_server_id,
        updates,
        database=database,
    )


def _delete_link(discord_server_id: int):
    return document_store.delete_google_sheet_link(discord_server_id)


@_links_transaction
def link_google_sheet_credentials(
    discord_server_id: int,
    guild_name: str,
    credentials_text: str,
    google_sheet_name: str = "",
    google_worksheet_name: str = "",
    lootsplit_history_worksheet_name: str = "",
    balance_history_worksheet_name: str = "",
) -> Tuple[bool, str]:
    clean_guild_name = str(guild_name or "").strip()
    if not clean_guild_name:
        return False, "**FAILED.** Albion guild name cannot be empty."
    try:
        parsed_credentials = json.loads(credentials_text)
    except json.JSONDecodeError:
        return False, "**FAILED.** Credentials text is not valid JSON."

    if not isinstance(parsed_credentials, dict):
        return False, "**FAILED.** Credentials JSON must be an object."

    required_keys = {"client_email", "private_key", "project_id"}
    missing_keys = [key for key in required_keys if key not in parsed_credentials]
    if missing_keys:
        return (
            False,
            f"**FAILED.** Credentials JSON is missing required key(s): {', '.join(missing_keys)}.",
        )

    resolved_sheet_name = google_sheet_name.strip() or clean_guild_name
    resolved_worksheet_name = google_worksheet_name.strip() or "Players"
    resolved_lootsplit_history_worksheet_name = (
        lootsplit_history_worksheet_name.strip() or "Lootsplit History"
    )
    resolved_balance_history_worksheet_name = (
        balance_history_worksheet_name.strip() or "Balance History"
    )

    sanitized_guild_name = _sanitize_guild_name(clean_guild_name)
    credentials_file_name = (
        f"{int(discord_server_id)}_{sanitized_guild_name}_{uuid4().hex}_credentials.json"
    )

    try:
        previous_link = _get_link(discord_server_id)
        credentials_file_path = _credentials_path(credentials_file_name)
        _write_private_json(credentials_file_path, parsed_credentials)
    except (
        document_store.DocumentCorruptionError,
        json.JSONDecodeError,
        OSError,
        sqlite3.Error,
        TypeError,
        ValueError,
    ):
        return False, "**FAILED.** Credentials could not be saved securely."

    link_payload = {
        "status": "active",
        "guild_name": clean_guild_name,
        "credentials_file": credentials_file_name,
        "google_sheet_name": resolved_sheet_name,
        "google_worksheet_name": resolved_worksheet_name,
        "lootsplit_history_worksheet_name": resolved_lootsplit_history_worksheet_name,
        "balance_history_worksheet_name": resolved_balance_history_worksheet_name,
    }
    try:
        previous_link = _upsert_link(discord_server_id, link_payload)
    except (
        document_store.DocumentCorruptionError,
        json.JSONDecodeError,
        OSError,
        sqlite3.Error,
        TypeError,
        ValueError,
    ):
        try:
            if credentials_file_path.exists() and not credentials_file_path.is_symlink():
                credentials_file_path.unlink()
        except OSError:
            pass
        return False, "**FAILED.** Credentials link metadata could not be saved securely."

    previous_file_name = (
        previous_link.get("credentials_file") if isinstance(previous_link, dict) else None
    )
    cleanup_warning = ""
    if previous_file_name and previous_file_name != credentials_file_name:
        try:
            _delete_credentials_file_if_unreferenced(previous_file_name)
        except (
            document_store.DocumentCorruptionError,
            json.JSONDecodeError,
            OSError,
            sqlite3.Error,
            TypeError,
            ValueError,
        ):
            cleanup_warning = (
                "\nWarning: the previous credentials file could not be checked "
                "for safe cleanup and was retained."
            )

    return (
        True,
        (
            f"**SUCCESS.** Google Sheet credentials were linked to this server. Saved file: **{credentials_file_name}**\n"
            f"Sheet: **{resolved_sheet_name}**\n"
            f"Worksheet: **{resolved_worksheet_name}**\n"
            f"Lootsplit Worksheet: **{resolved_lootsplit_history_worksheet_name}**\n"
            f"Balance History Worksheet: **{resolved_balance_history_worksheet_name}**"
            f"{cleanup_warning}"
        ),
    )


@_links_transaction
def remove_google_sheet_credentials(discord_server_id: int) -> None:
    link_info = _delete_link(discord_server_id)
    if link_info is None:
        return

    credentials_file_name = (
        link_info.get("credentials_file") if isinstance(link_info, dict) else None
    )
    if credentials_file_name:
        _delete_credentials_file_if_unreferenced(credentials_file_name)


def _delete_credentials_file_if_unreferenced(file_name: object) -> None:
    """Delete a credential only when no other guild link still references it."""

    clean_name = str(file_name or "").strip()
    if not clean_name:
        return
    if document_store.is_google_credentials_file_referenced(clean_name):
        return

    try:
        credentials_file_path = _credentials_path(clean_name)
    except (OSError, ValueError):
        return
    if credentials_file_path.exists() and not credentials_file_path.is_symlink():
        credentials_file_path.unlink()


@_links_transaction
def get_credentials_info(discord_server_id: int) -> dict:
    link_info = _get_link(discord_server_id)
    if not isinstance(link_info, dict):
        return {}
    status = str(link_info.get("status") or "active").strip().casefold()
    if status != "active":
        return {}
    if link_info.get("disabled") is True or link_info.get("quarantined") is True:
        return {}
    guild_name = str(link_info.get("guild_name") or "").strip()
    if not guild_name:
        return {}
    credentials_file_name = link_info.get("credentials_file")

    if not credentials_file_name:
        return {}

    try:
        credentials_file_path = _credentials_path(
            credentials_file_name,
            require_existing=True,
        )
    except (OSError, ValueError):
        return {}

    try:
        os.chmod(credentials_file_path, 0o600)
    except OSError:
        return {}

    return {
        "status": "active",
        "guild_name": guild_name,
        "credentials_file": str(credentials_file_path),
        "google_sheet_name": str(link_info.get("google_sheet_name") or guild_name).strip(),
        "google_worksheet_name": str(link_info.get("google_worksheet_name") or "Players").strip(),
        "lootsplit_history_worksheet_name": str(
            link_info.get("lootsplit_history_worksheet_name") or "Lootsplit History"
        ).strip(),
        "balance_history_worksheet_name": str(
            link_info.get("balance_history_worksheet_name") or "Balance History"
        ).strip(),
        "spreadsheet_id": link_info.get("spreadsheet_id"),
        "players_worksheet_id": link_info.get("players_worksheet_id"),
        "lootsplit_history_worksheet_id": link_info.get("lootsplit_history_worksheet_id"),
        "balance_history_worksheet_id": link_info.get("balance_history_worksheet_id"),
    }


@_links_transaction
def quarantine_google_sheet_link(
    discord_server_id: int,
    reason: str,
    *,
    database=None,
) -> Optional[dict]:
    """Disable one link, optionally inside the caller's SQLite transaction."""

    clean_reason = str(reason or "").strip()
    if not clean_reason:
        raise ValueError("Quarantine reason cannot be empty.")
    return _update_link_fields(
        discord_server_id,
        {
            "status": "quarantined",
            "quarantine_reason": clean_reason[:1000],
            "quarantined_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        },
        database=database,
    )


@_links_transaction
def record_google_resource_ids(
    discord_server_id: int,
    *,
    spreadsheet_id: object,
    worksheet_type: str,
    worksheet_id: object,
) -> bool:
    """Persist stable physical Google IDs after a successful name-based lookup."""

    link_info = _get_link(discord_server_id)
    if not isinstance(link_info, dict):
        return False
    status = str(link_info.get("status") or "active").strip().casefold()
    if status != "active":
        return False
    if link_info.get("disabled") is True or link_info.get("quarantined") is True:
        return False
    worksheet_fields = {
        "players": "players_worksheet_id",
        "lootsplit_history": "lootsplit_history_worksheet_id",
        "balance_history": "balance_history_worksheet_id",
    }
    worksheet_field = worksheet_fields.get(str(worksheet_type or "").strip())
    if worksheet_field is None:
        raise ValueError("Unsupported worksheet type.")
    clean_spreadsheet_id = str(spreadsheet_id or "").strip()
    if not clean_spreadsheet_id:
        raise ValueError("spreadsheet_id cannot be empty")
    if isinstance(worksheet_id, bool):
        raise ValueError("worksheet_id must be an integer")
    if isinstance(worksheet_id, int):
        clean_worksheet_id = worksheet_id
    elif isinstance(worksheet_id, str):
        try:
            clean_worksheet_id = int(worksheet_id.strip())
        except ValueError as error:
            raise ValueError("worksheet_id must be an integer") from error
    else:
        raise ValueError("worksheet_id must be an integer")
    if clean_worksheet_id < 0:
        raise ValueError("worksheet_id must not be negative")
    updates = {
        "spreadsheet_id": clean_spreadsheet_id,
        worksheet_field: clean_worksheet_id,
    }
    if (
        link_info.get("spreadsheet_id") == updates["spreadsheet_id"]
        and link_info.get(worksheet_field) == updates[worksheet_field]
    ):
        return True
    return _update_link_fields(discord_server_id, updates) is not None


@_links_transaction
def update_credentials_link_field(
    discord_server_id: int, field_name: str, new_value: str
) -> Tuple[bool, str]:
    link_info = _get_link(discord_server_id)

    if not isinstance(link_info, dict):
        return False, "Google Sheet is not linked yet. Run **/bot-link-google-sheet** first."
    status = str(link_info.get("status") or "active").strip().casefold()
    if status != "active":
        return False, "Google Sheet link is disabled. Relink it before updating configuration."
    if link_info.get("disabled") is True or link_info.get("quarantined") is True:
        return False, "Google Sheet link is disabled. Relink it before updating configuration."

    allowed_fields = {
        "credentials_file",
        "google_sheet_name",
        "google_worksheet_name",
        "lootsplit_history_worksheet_name",
        "balance_history_worksheet_name",
    }
    if field_name not in allowed_fields:
        return False, "Unsupported configuration field."

    clean_value = new_value.strip()
    if not clean_value:
        return False, "Value cannot be empty."

    if field_name == "credentials_file":
        current_file_name = str(link_info.get("credentials_file") or "").strip()
        if clean_value != current_file_name and not clean_value.startswith(
            f"{int(discord_server_id)}_"
        ):
            return (
                False,
                "Credentials are isolated per Discord server. Use "
                "**/bot-link-google-sheet** to replace them securely.",
            )
        try:
            _credentials_path(clean_value, require_existing=True)
        except ValueError:
            return False, "Credentials file must be a file name from `google_sheet_credentials/`."
        except OSError:
            return (
                False,
                f"Credentials file **{clean_value}** was not found in `google_sheet_credentials/`.",
            )

    updates: dict = {field_name: clean_value}
    if field_name == "google_sheet_name":
        updates.update(
            {
                "spreadsheet_id": None,
                "players_worksheet_id": None,
                "lootsplit_history_worksheet_id": None,
                "balance_history_worksheet_id": None,
            }
        )
    elif field_name == "google_worksheet_name":
        updates["players_worksheet_id"] = None
    elif field_name == "lootsplit_history_worksheet_name":
        updates["lootsplit_history_worksheet_id"] = None
    elif field_name == "balance_history_worksheet_name":
        updates["balance_history_worksheet_id"] = None

    updated_link = _update_link_fields(
        discord_server_id,
        updates,
    )
    if updated_link is None:
        return False, "Google Sheet is not linked yet. Run **/bot-link-google-sheet** first."
    return True, "Configuration updated."
