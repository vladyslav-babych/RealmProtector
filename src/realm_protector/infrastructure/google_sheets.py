import logging
import time
from typing import Any, Callable, Iterable, Optional, Protocol, TypeVar

import gspread
from google.oauth2.service_account import Credentials

from src.realm_protector.infrastructure import credential_store

T = TypeVar("T")


class CellPort(Protocol):
    value: Any


class WorksheetPort(Protocol):
    """Synchronous worksheet surface used by the Google adapter and projection."""

    def col_values(self, col_index: int) -> list[str]: ...

    def cell(self, row_index: int, col_index: int) -> CellPort: ...

    def get_all_values(self, **kwargs: Any) -> list[list[Any]]: ...

    def row_values(self, row_index: int) -> list[str]: ...

    def update_cell(self, row_index: int, col_index: int, value: str) -> Any: ...

    def update(self, range_name: str, values: list[list[Any]], **kwargs: Any) -> Any: ...

    def append_row(self, values: list[Any], **kwargs: Any) -> Any: ...

    def append_rows(self, values: list[list[Any]], **kwargs: Any) -> Any: ...

    def batch_update(self, requests: list[dict[str, Any]], **kwargs: Any) -> Any: ...


GOOGLE_SHEET_SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets,"
    "https://www.googleapis.com/auth/drive.metadata.readonly"
)
WORKSHEET_TYPE_PLAYERS = "players"
WORKSHEET_TYPE_LOOTSPLIT_HISTORY = "lootsplit_history"
WORKSHEET_TYPE_BALANCE_HISTORY = "balance_history"
GOOGLE_HTTP_TIMEOUT = (10.0, 30.0)

LOOTSPLIT_HISTORY_HEADERS = [
    "Battleboard ID",
    "Date",
    "Officer",
    "Content name",
    "Caller",
    "Participant",
    "Lootsplit",
]

BALANCE_HISTORY_HEADERS = [
    "Date",
    "Reason",
    "Officer",
    "Nickname",
    "Amount",
]
PLAYERS_REQUIRED_HEADERS = [
    "Discord ID",
    "Albion Nickname",
    "Is In Guild",
    "Silver",
]


class WorksheetSchemaError(ValueError):
    """Raised when a non-empty worksheet has unexpected headers."""


def _parse_scopes(raw_scopes: Optional[str]) -> list[str]:
    return [scope.strip() for scope in str(raw_scopes or "").split(",") if scope.strip()]


def _resolve_worksheet_name(creds_info: dict, worksheet_type: str) -> str:
    if worksheet_type == WORKSHEET_TYPE_LOOTSPLIT_HISTORY:
        configured = creds_info.get("lootsplit_history_worksheet_name")
        default = "Lootsplit History"

    elif worksheet_type == WORKSHEET_TYPE_BALANCE_HISTORY:
        configured = creds_info.get("balance_history_worksheet_name")
        default = "Balance History"

    elif worksheet_type == WORKSHEET_TYPE_PLAYERS:
        configured = creds_info.get("google_worksheet_name")
        default = "Players"

    else:
        raise ValueError(f"Unsupported worksheet type: {worksheet_type}")

    worksheet_name = str(configured or default).strip()
    if not worksheet_name:
        raise ValueError("Google worksheet name cannot be empty.")
    return worksheet_name


def _worksheet_id_field(worksheet_type: str) -> str:
    fields = {
        WORKSHEET_TYPE_PLAYERS: "players_worksheet_id",
        WORKSHEET_TYPE_LOOTSPLIT_HISTORY: "lootsplit_history_worksheet_id",
        WORKSHEET_TYPE_BALANCE_HISTORY: "balance_history_worksheet_id",
    }
    try:
        return fields[worksheet_type]
    except KeyError as error:
        raise ValueError(f"Unsupported worksheet type: {worksheet_type}") from error


def _is_quota_error(error: Exception) -> bool:
    if not isinstance(error, gspread.exceptions.APIError):
        return False

    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code == 429:
        return True

    return "429" in str(error) or "quota" in str(error).lower()


def _call_with_backoff(
    operation: Callable[[], T],
    attempts: int = 5,
    initial_delay_seconds: float = 1.0,
) -> T:
    if attempts <= 0:
        raise ValueError("attempts must be positive")
    last_error: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            return operation()
        except Exception as error:
            last_error = error
            if not _is_quota_error(error) or attempt == attempts - 1:
                raise

            time.sleep(initial_delay_seconds * (2**attempt))

    if last_error is None:
        raise RuntimeError("Google operation ended without a result or error.")
    raise last_error


def _open_spreadsheet(client, creds_info: dict):
    spreadsheet_id = str(creds_info.get("spreadsheet_id") or "").strip()
    if spreadsheet_id:
        return client.open_by_key(spreadsheet_id)
    return client.open(creds_info.get("google_sheet_name"))


def _resolve_worksheet(spreadsheet, creds_info: dict, worksheet_type: str):
    worksheet_id = creds_info.get(_worksheet_id_field(worksheet_type))
    if worksheet_id is not None:
        return spreadsheet.get_worksheet_by_id(int(worksheet_id))
    return spreadsheet.worksheet(_resolve_worksheet_name(creds_info, worksheet_type))


def get_worksheets(
    discord_server_id: int,
    worksheet_types: Iterable[str] = (
        WORKSHEET_TYPE_PLAYERS,
        WORKSHEET_TYPE_LOOTSPLIT_HISTORY,
        WORKSHEET_TYPE_BALANCE_HISTORY,
    ),
) -> dict[str, WorksheetPort]:
    """Resolve several tabs with one authorization and spreadsheet lookup."""

    creds_info = credential_store.get_credentials_info(discord_server_id)
    if not creds_info:
        raise ValueError("Google Sheet is not actively linked for this Discord server.")
    types = tuple(dict.fromkeys(str(value) for value in worksheet_types))
    for worksheet_type in types:
        _worksheet_id_field(worksheet_type)

    scopes = _parse_scopes(GOOGLE_SHEET_SCOPES)
    creds = Credentials.from_service_account_file(
        creds_info.get("credentials_file"),
        scopes=scopes,
    )
    client = gspread.authorize(creds)
    client.http_client.timeout = GOOGLE_HTTP_TIMEOUT
    spreadsheet = _open_spreadsheet(client, creds_info)
    resolved: dict[str, WorksheetPort] = {}
    for worksheet_type in types:
        worksheet = _resolve_worksheet(spreadsheet, creds_info, worksheet_type)
        if worksheet_type == WORKSHEET_TYPE_PLAYERS:
            validate_players_headers(worksheet)
        resolved[worksheet_type] = worksheet
        spreadsheet_id = getattr(spreadsheet, "id", None)
        worksheet_id = getattr(worksheet, "id", None)
        if spreadsheet_id is not None and worksheet_id is not None:
            try:
                credential_store.record_google_resource_ids(
                    discord_server_id,
                    spreadsheet_id=spreadsheet_id,
                    worksheet_type=worksheet_type,
                    worksheet_id=worksheet_id,
                )
            except Exception:
                logging.exception("Could not persist stable Google resource IDs")
    return resolved


def get_worksheet(
    discord_server_id: Optional[int] = None,
    worksheet_type: str = WORKSHEET_TYPE_PLAYERS,
) -> WorksheetPort:
    if discord_server_id is None:
        raise ValueError("discord_server_id is required")
    return get_worksheets(discord_server_id, (worksheet_type,))[worksheet_type]


def validate_players_headers(worksheet: WorksheetPort) -> None:
    first_row = _call_with_backoff(lambda: worksheet.row_values(1))
    actual_required = [str(value or "").strip() for value in first_row[:4]]
    if actual_required != PLAYERS_REQUIRED_HEADERS:
        raise WorksheetSchemaError(
            "Players headers must start with: " + ", ".join(PLAYERS_REQUIRED_HEADERS)
        )
    if len(first_row) >= 5 and str(first_row[4] or "").strip() not in {"", "Siphon"}:
        raise WorksheetSchemaError("The optional fifth Players header must be Siphon.")


def get_lootsplit_history_headers() -> list[str]:
    return LOOTSPLIT_HISTORY_HEADERS.copy()


def ensure_lootsplit_history_headers(worksheet: WorksheetPort):
    headers = get_lootsplit_history_headers()
    first_row = _call_with_backoff(lambda: worksheet.row_values(1))
    first_row_padded = first_row[: len(headers)] + [""] * max(0, len(headers) - len(first_row))

    if first_row_padded == headers:
        return
    if any(str(value or "").strip() for value in first_row):
        raise WorksheetSchemaError("Lootsplit History headers do not match the required schema.")
    _call_with_backoff(lambda: worksheet.update("A1:G1", [headers]))


def ensure_balance_history_headers(worksheet: WorksheetPort):
    first_row = _call_with_backoff(lambda: worksheet.row_values(1))
    first_row_padded = first_row[: len(BALANCE_HISTORY_HEADERS)] + [""] * max(
        0, len(BALANCE_HISTORY_HEADERS) - len(first_row)
    )

    if first_row_padded == BALANCE_HISTORY_HEADERS:
        return
    if any(str(value or "").strip() for value in first_row):
        raise WorksheetSchemaError("Balance History headers do not match the required schema.")
    _call_with_backoff(lambda: worksheet.update("A1:E1", [BALANCE_HISTORY_HEADERS]))
