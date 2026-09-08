#!/usr/bin/env python3
"""Preview the known registration-link repair; normal bot startup applies it once."""

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

from src.realm_protector.infrastructure import sqlite_database, startup_repairs  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--database", type=Path, default=None)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply now instead of previewing. Stop the bot first. Not needed for automatic startup repair.",
    )
    arguments = parser.parse_args()
    project_root = arguments.project_root.expanduser().resolve()
    load_dotenv(project_root / ".env")
    database_path = sqlite_database.resolve_project_database_path(
        project_root, arguments.database or sqlite_database.get_database_path()
    )
    os.umask(0o077)
    results = startup_repairs.run_startup_repairs(database_path, dry_run=not arguments.apply)
    print(
        json.dumps(
            {
                "database_path": str(database_path),
                "dry_run": not arguments.apply,
                "repairs": [result.to_dict() for result in results],
            },
            indent=2,
        )
    )
    return 1 if any(result.status == "blocked" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
