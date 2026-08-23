import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.realm_protector.infrastructure import google_sheets

WorksheetSchemaError = google_sheets.WorksheetSchemaError
validate_players_headers = google_sheets.validate_players_headers


class HeaderWorksheet:
    def __init__(self, headers: list[str]) -> None:
        self.headers = headers
        self.requested_rows: list[int] = []

    def row_values(self, row_index: int) -> list[str]:
        self.requested_rows.append(row_index)
        return list(self.headers)


class PlayersHeaderValidationTests(unittest.TestCase):
    def test_accepts_only_the_required_prefix_and_optional_siphon_header(self) -> None:
        required = ["Discord ID", "Albion Nickname", "Is In Guild", "Silver"]

        for headers in (required, [*required, ""], [*required, "Siphon"]):
            with self.subTest(headers=headers):
                worksheet = HeaderWorksheet(headers)
                validate_players_headers(worksheet)
                self.assertEqual([1], worksheet.requested_rows)

    def test_empty_or_partial_players_sheet_fails_closed(self) -> None:
        invalid_headers = (
            [],
            ["Discord ID"],
            ["Discord ID", "Albion Nickname", "Is In Guild"],
        )

        for headers in invalid_headers:
            with self.subTest(headers=headers):
                with self.assertRaises(WorksheetSchemaError):
                    validate_players_headers(HeaderWorksheet(list(headers)))

    def test_reordered_or_misspelled_required_headers_fail_closed(self) -> None:
        invalid_headers = (
            ["Albion Nickname", "Discord ID", "Is In Guild", "Silver"],
            ["Discord ID", "Albion Nickname", "In Guild", "Silver"],
            ["discord id", "Albion Nickname", "Is In Guild", "Silver"],
        )

        for headers in invalid_headers:
            with self.subTest(headers=headers):
                with self.assertRaises(WorksheetSchemaError):
                    validate_players_headers(HeaderWorksheet(list(headers)))

    def test_unexpected_optional_fifth_header_fails_closed(self) -> None:
        worksheet = HeaderWorksheet(
            [
                "Discord ID",
                "Albion Nickname",
                "Is In Guild",
                "Silver",
                "Balance",
            ]
        )

        with self.assertRaisesRegex(
            WorksheetSchemaError,
            "optional fifth Players header must be Siphon",
        ):
            validate_players_headers(worksheet)

    def test_google_client_uses_explicit_connect_and_read_timeouts(self) -> None:
        worksheet = HeaderWorksheet(list(google_sheets.PLAYERS_REQUIRED_HEADERS))
        spreadsheet = SimpleNamespace(worksheet=Mock(return_value=worksheet))
        client = SimpleNamespace(
            http_client=SimpleNamespace(timeout=None),
            open=Mock(return_value=spreadsheet),
        )

        with (
            patch.object(
                google_sheets.credential_store,
                "get_credentials_info",
                return_value={
                    "credentials_file": "/private/credentials.json",
                    "google_sheet_name": "Realm Ledger",
                    "google_worksheet_name": "Players",
                },
            ),
            patch.object(
                google_sheets.Credentials,
                "from_service_account_file",
                return_value=object(),
            ),
            patch.object(google_sheets.gspread, "authorize", return_value=client),
        ):
            resolved = google_sheets.get_worksheet(42)

        self.assertIs(worksheet, resolved)
        self.assertEqual(
            google_sheets.GOOGLE_HTTP_TIMEOUT,
            client.http_client.timeout,
        )

    def test_stable_resource_ids_use_open_by_key_and_worksheet_id(self) -> None:
        worksheet = HeaderWorksheet(list(google_sheets.PLAYERS_REQUIRED_HEADERS))
        worksheet.id = 123
        spreadsheet = SimpleNamespace(
            id="stable-sheet-key",
            get_worksheet_by_id=Mock(return_value=worksheet),
        )
        client = SimpleNamespace(
            http_client=SimpleNamespace(timeout=None),
            open=Mock(),
            open_by_key=Mock(return_value=spreadsheet),
        )

        with (
            patch.object(
                google_sheets.credential_store,
                "get_credentials_info",
                return_value={
                    "credentials_file": "/private/credentials.json",
                    "google_sheet_name": "Mutable title",
                    "google_worksheet_name": "Mutable tab",
                    "spreadsheet_id": "stable-sheet-key",
                    "players_worksheet_id": 123,
                },
            ),
            patch.object(
                google_sheets.credential_store,
                "record_google_resource_ids",
                return_value=True,
            ) as record_ids,
            patch.object(
                google_sheets.Credentials,
                "from_service_account_file",
                return_value=object(),
            ),
            patch.object(google_sheets.gspread, "authorize", return_value=client),
        ):
            resolved = google_sheets.get_worksheet(42)

        self.assertIs(worksheet, resolved)
        client.open_by_key.assert_called_once_with("stable-sheet-key")
        client.open.assert_not_called()
        spreadsheet.get_worksheet_by_id.assert_called_once_with(123)
        record_ids.assert_called_once()


if __name__ == "__main__":
    unittest.main()
