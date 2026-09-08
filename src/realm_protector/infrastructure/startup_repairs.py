"""Narrow, audited data repairs applied before Discord/background services start.

These are data migrations, not general account transfers. Every repair matches
the original server, ledger, Discord account, nickname and incorrect character ID.
The data change and its completion record commit together, or neither commits.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from src.realm_protector.infrastructure import runtime_state, sqlite_database

LOGGER = logging.getLogger(__name__)
REPAIR_KIND = "startup_data_repair"
JIMMYCOACAZA_REPAIR_ID = "2026-09-08-jimmycoacaza-character-id"
DISCORD_GUILD_ID = 1540015310147555508
LEDGER_ID = 1540015310147555508
DISCORD_USER_ID = 289072837749112843
NICKNAME = "JimmyCoacaza"
WRONG_ALBION_ID = "rnEfd5QzSlm2qSQHOcMwBQ"
CORRECT_ALBION_ID = "JG92lq90TAyFfq_FL6OeMQ"
# Both identities were verified against Albion Europe's public API on 2026-09-08.
# The wrong ID belongs to Mamaliga; this repair does not register/transfer Mamaliga.


@dataclass(frozen=True)
class StartupRepairResult:
    repair_id: str
    status: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def _result(status: str, message: str) -> StartupRepairResult:
    return StartupRepairResult(JIMMYCOACAZA_REPAIR_ID, status, message)


def _inspect_repair(database: sqlite3.Connection) -> tuple[StartupRepairResult, dict | None]:
    completion = database.execute(
        "SELECT status FROM runtime_records WHERE kind = ? AND guild_id = ? AND external_id = ?",
        (REPAIR_KIND, DISCORD_GUILD_ID, JIMMYCOACAZA_REPAIR_ID),
    ).fetchone()
    if completion is not None and completion["status"] == "completed":
        return _result("already_applied", "This one-time repair has already completed."), None

    ledger = database.execute(
        "SELECT * FROM guild_ledger_generations WHERE ledger_id = ?", (LEDGER_ID,)
    ).fetchone()
    if ledger is None:
        return _result(
            "not_applicable", "The affected ledger is not present in this database."
        ), None
    if (
        int(ledger["discord_guild_id"]) != DISCORD_GUILD_ID
        or ledger["status"] != "active"
        or ledger["target_guild_key"] != "team casualty"
    ):
        return _result(
            "blocked",
            "The original TEAM CASUALTY ledger is no longer active or its identity changed; no player was modified.",
        ), None

    row = database.execute(
        "SELECT * FROM registered_players WHERE guild_id = ? AND discord_user_id = ?",
        (LEDGER_ID, DISCORD_USER_ID),
    ).fetchone()
    if row is None:
        return _result(
            "not_applicable", "The affected Discord account is not registered in this ledger."
        ), None
    original = dict(row)
    if original["nickname"] != NICKNAME or original["nickname_key"] != "jimmycoacaza":
        return _result(
            "blocked",
            "The account's nickname changed; refusing to overwrite a different registration.",
        ), original
    if original["albion_player_id"] == CORRECT_ALBION_ID:
        return _result(
            "already_correct", "JimmyCoacaza already has the verified character ID."
        ), original
    if original["albion_player_id"] != WRONG_ALBION_ID:
        return _result(
            "blocked",
            "The stored character ID differs from the known incorrect ID; no player was modified.",
        ), original

    owner = database.execute(
        "SELECT discord_user_id FROM registered_players "
        "WHERE guild_id = ? AND albion_player_id = ? AND discord_user_id != ?",
        (LEDGER_ID, CORRECT_ALBION_ID, DISCORD_USER_ID),
    ).fetchone()
    if owner is not None:
        return _result(
            "blocked",
            f"JimmyCoacaza's correct character ID is already owned by Discord ID {owner['discord_user_id']} in this ledger; no player was modified.",
        ), original
    return _result(
        "would_apply",
        "Only JimmyCoacaza's incorrect Albion character ID would be corrected; every other player field is preserved.",
    ), original


def _apply_repair(database: sqlite3.Connection) -> StartupRepairResult:
    result, original = _inspect_repair(database)
    if result.status in {"already_applied", "not_applicable"}:
        return result
    corrected = original
    if result.status == "would_apply":
        assert original is not None
        cursor = database.execute(
            "UPDATE registered_players SET albion_player_id = ? "
            "WHERE guild_id = ? AND discord_user_id = ? AND nickname = ? "
            "AND nickname_key = ? AND albion_player_id = ?",
            (
                CORRECT_ALBION_ID,
                LEDGER_ID,
                DISCORD_USER_ID,
                NICKNAME,
                "jimmycoacaza",
                WRONG_ALBION_ID,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(
                "The registration repair did not match exactly one player; rolling back."
            )
        row = database.execute(
            "SELECT * FROM registered_players WHERE guild_id = ? AND discord_user_id = ?",
            (LEDGER_ID, DISCORD_USER_ID),
        ).fetchone()
        corrected = dict(row) if row is not None else None
        if corrected != {**original, "albion_player_id": CORRECT_ALBION_ID}:
            raise RuntimeError("The repair changed fields other than the Albion ID; rolling back.")
        result = _result(
            "applied",
            "JimmyCoacaza's Albion ID was corrected. Balances, earnings, status, revisions, Siphon and history were preserved. Retry /force-register for Mamaliga.",
        )

    runtime_state.upsert_record_in_transaction(
        database,
        REPAIR_KIND,
        DISCORD_GUILD_ID,
        JIMMYCOACAZA_REPAIR_ID,
        {
            "repair_id": JIMMYCOACAZA_REPAIR_ID,
            "ledger_id": LEDGER_ID,
            "discord_user_id": DISCORD_USER_ID,
            "expected_nickname": NICKNAME,
            "previous_albion_player_id": WRONG_ALBION_ID,
            "correct_albion_player_id": CORRECT_ALBION_ID,
            "outcome": result.status,
            "message": result.message,
            "before": original,
            "after": corrected,
        },
        status="blocked" if result.status == "blocked" else "completed",
    )
    return result


def run_startup_repairs(
    database_path: str | Path | None = None, *, dry_run: bool = False
) -> tuple[StartupRepairResult, ...]:
    """Run after schema initialization; dry-run does not migrate or change rows.

    No network or Google sync is needed: Albion IDs were independently verified,
    and the only changed field is not projected into Google Sheets. There is no
    automatic Discord account transfer or registration of the target player.
    """
    path = Path(database_path or sqlite_database.get_database_path()).expanduser()
    if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
        raise ValueError("Repairs require an existing regular SQLite file, not a symbolic link.")
    if dry_run:
        # Use normal read-only SQLite, including any WAL; never use immutable=1
        # against a possibly live database because that would ignore committed WAL data.
        database = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            database.row_factory = sqlite3.Row
            database.execute("BEGIN")
            result, _ = _inspect_repair(database)
        finally:
            database.close()
    else:
        with sqlite_database.transaction(path) as database:
            result = _apply_repair(database)
    if result.status == "blocked":
        LOGGER.error("Startup data repair %s: %s", result.repair_id, result.message)
    elif result.status == "applied":
        # Storage initialization precedes Discord's log setup. WARNING ensures
        # the one-time success is visible in the host's startup/console logs.
        LOGGER.warning("Startup data repair %s applied: %s", result.repair_id, result.message)
    else:
        LOGGER.info(
            "Startup data repair %s [%s]: %s", result.repair_id, result.status, result.message
        )
    return (result,)
