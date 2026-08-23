"""Executable entry point for Realm Protector."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Mapping, Optional

from dotenv import load_dotenv

from src.realm_protector.bot.client import RealmProtectorBot
from src.realm_protector.infrastructure import (
    guild_settings,
    local_repository,
    sqlite_database,
)
from src.realm_protector.infrastructure.credential_store import (
    harden_existing_credential_permissions,
)
from src.realm_protector.infrastructure.legacy_migration import (
    LegacyMigrationReport,
    migrate_legacy_storage,
)

PROJECT_ROOT = Path(__file__).resolve().parent
PRODUCTION_ENVIRONMENTS = {"prod", "production"}
NON_PRODUCTION_ENVIRONMENTS = {"dev", "development", "test", "testing"}


def initialize_local_storage(project_root: Path = PROJECT_ROOT) -> LegacyMigrationReport:
    """Open the authoritative SQLite database and import legacy JSON once."""

    configured_path = sqlite_database.resolve_project_database_path(
        project_root,
        sqlite_database.get_database_path(),
    )
    sqlite_database.configure_database(configured_path)
    local_repository.ensure_schema(configured_path)
    report = migrate_legacy_storage(
        project_root,
        database_path=configured_path,
    )
    if report.failed:
        failed_sources = ", ".join(source.source_path for source in report.sources if source.failed)
        raise RuntimeError("Legacy storage migration failed for: " + failed_sources)
    guild_settings.reconcile_ledger_generations()
    return report


def resolve_discord_token(environment: Optional[Mapping[str, str]] = None) -> str:
    """Select the configured production or test token without logging its value."""

    values = environment if environment is not None else os.environ
    bot_environment = values.get("BOT_ENV", "").strip().casefold()

    if not bot_environment or bot_environment in PRODUCTION_ENVIRONMENTS:
        variable_name = "DISCORD_TOKEN"
        token = values.get(variable_name)
    elif bot_environment in NON_PRODUCTION_ENVIRONMENTS:
        variable_name = "DISCORD_TOKEN_TEST"
        token = values.get(variable_name)
    else:
        raise RuntimeError("Unsupported BOT_ENV. Use production, development, or test.")

    if not token or not token.strip():
        raise RuntimeError(
            f"Missing {variable_name}. Add it to the environment or the project .env file."
        )
    return token.strip()


def main() -> None:
    os.umask(0o077)
    os.chdir(PROJECT_ROOT)
    environment_file = PROJECT_ROOT / ".env"
    if environment_file.is_file() and not environment_file.is_symlink():
        environment_file.chmod(0o600)
    config_directory = PROJECT_ROOT / "configs"
    if config_directory.is_dir() and not config_directory.is_symlink():
        config_directory.chmod(0o700)
        for config_file in config_directory.glob("*.json"):
            if config_file.is_file() and not config_file.is_symlink():
                config_file.chmod(0o600)
    harden_existing_credential_permissions()
    load_dotenv(environment_file)
    initialize_local_storage(PROJECT_ROOT)
    token = resolve_discord_token()
    active_log_path = PROJECT_ROOT / "discord.log"
    if active_log_path.is_symlink():
        raise RuntimeError("Refusing to write logs through a discord.log symbolic link.")
    for log_path in PROJECT_ROOT.glob("discord.log*"):
        if log_path.is_file() and not log_path.is_symlink():
            log_path.chmod(0o600)
    log_handler = RotatingFileHandler(
        active_log_path,
        encoding="utf-8",
        mode="a",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
    )
    active_log_path.chmod(0o600)
    bot = RealmProtectorBot(PROJECT_ROOT)
    bot.run(token, log_handler=log_handler, log_level=logging.INFO)


if __name__ == "__main__":
    main()
