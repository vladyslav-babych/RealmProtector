#!/usr/bin/env python3
"""Import Realm Protector's legacy JSON and linked Google data into SQLite."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.realm_protector.infrastructure import (  # noqa: E402
    guild_settings,
    local_repository,
    sqlite_database,
)
from src.realm_protector.infrastructure.legacy_migration import (  # noqa: E402
    migrate_legacy_storage,
)
from src.realm_protector.services import google_sync  # noqa: E402


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Import legacy Realm Protector JSON configuration and linked Google "
            "data into SQLite without changing local source files."
        )
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Project root containing configs/ and google_sheet_credentials/.",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help="SQLite file. Relative paths are resolved below --project-root.",
    )
    parser.add_argument(
        "--skip-google",
        action="store_true",
        help="Import local JSON only; do not contact linked Google Sheets.",
    )
    return parser.parse_args()


def main() -> int:
    arguments = _parse_arguments()
    project_root = arguments.project_root.expanduser().resolve()
    os.chdir(project_root)
    load_dotenv(project_root / ".env")
    database_path = arguments.database
    if database_path is None:
        database_path = sqlite_database.get_database_path()
    database_path = sqlite_database.resolve_project_database_path(
        project_root,
        database_path,
    )

    sqlite_database.configure_database(database_path)
    local_repository.ensure_schema(database_path)
    report = migrate_legacy_storage(
        project_root,
        database_path=database_path,
    )
    if not report.failed:
        guild_settings.reconcile_ledger_generations()
    output = report.to_dict()
    google_failed = False
    if not arguments.skip_google and not report.failed:
        google_results = google_sync.bootstrap_all_linked_sheets()
        output["google_sheets"] = {
            str(guild_id): {
                "success": result.success,
                "message": result.message,
                "imported_rows": result.imported_rows,
            }
            for guild_id, result in google_results.items()
        }
        google_failed = any(not result.success for result in google_results.values())
    print(json.dumps(output, indent=2, ensure_ascii=True))
    return 1 if report.failed or google_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
