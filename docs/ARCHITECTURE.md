# Realm Protector architecture

Realm Protector follows the top-level organization. Discord
presentation, application workflows, deterministic domain policy, and external
persistence have explicit homes so storage and integrations can evolve without
changing the command surface. It is a pragmatic layered architecture: command
handlers still orchestrate some repositories directly, so this document does
not claim pure dependency inversion.

## Package map

```text
Realm Protector/
├── main.py                         # import-safe process entry point
├── scripts/migrate_to_sqlite.py    # explicit, restart-safe migration command
├── resources/messages/             # operator-editable presentation copy
├── src/realm_protector/
│   ├── bot/                         # Discord commands, views, listeners, composition root
│   ├── domain/                      # stdlib-only models and deterministic policies
│   ├── services/                    # application workflows and synchronization policy
│   └── infrastructure/              # SQLite repositories, Albion, Google, credentials
└── tests/                           # offline unit and composition tests
```

`main.py` owns process concerns only: private file permissions, stable project paths, environment loading, logging, storage initialization, migration, and bot startup. Importing it never opens a Discord connection.

`bot/client.py` is the Discord composition root. It registers commands, persistent views, listeners, startup reconciliation, and background jobs. Presentation modules validate Discord inputs and format responses.

`domain` contains policies and value objects without Discord, Google, HTTP, or filesystem imports.

`services` coordinates registration, membership audits, lifecycle/locking,
retention, UTC scheduling, request limits, and Google synchronization. Economy
commands currently invoke the transactional local repository directly.

`infrastructure` owns SQLite connections and repositories, legacy import adapters, Albion HTTP, gspread access, bounded blocking-I/O executors, and private Google credential files.

## Dependency rule

```text
Discord presentation ──> services ──> domain
        │                    │
        └──────────────> infrastructure <──┘
```

Domain policy never imports an outward layer. SQLite repositories do not import Discord or Google clients. Discord and Google calls are never awaited while a SQLite write transaction is open.

## Authoritative persistence

SQLite is the source of truth. The default database is `data/realm_protector.sqlite3`; `REALM_PROTECTOR_DATABASE_PATH` can override it. The database stores:

- guild setup, role IDs, timer settings, and persistent message references;
- ticket, reaction-role, and objective panel configuration;
- active objective scheduler state;
- registered Albion players, membership state, current Silver, cumulative
  all-time earnings from positive local credits, and cached Siphon;
- immutable balance and lootsplit history;
- open/archived ticket snapshots and active composition content;
- pending Discord publication/cleanup/notification actions;
- Google link metadata, ledger generations, immutable import snapshots,
  migration issues, and the sync outbox.

Service-account credential payloads remain separate owner-only JSON secret files. SQLite stores only their confined filename and Sheet mapping; secrets are not copied into the database.

SQLite runs with foreign keys, WAL journaling, a busy timeout, and owner-only permissions. Balance and imported history rows are immutable, and economy state plus its outbox event commit in one local transaction.

## Restart and migration lifecycle

Before constructing the Discord bot, every startup:

1. creates or upgrades the SQLite schema;
2. imports each legacy configuration JSON source once;
3. imports link metadata while leaving credentials in the private credential store;
4. constructs and starts Discord after local initialization succeeds.

At Discord readiness, the Google worker starts immediately and attempts the
one-time cutover for every active linked Sheet. A Google outage therefore does
not prevent login; linked ledger mutations remain gated until that ledger's
cutover succeeds. The explicit migration script can still request Google import
synchronously when an operator wants a preflight report.

Legacy JSON imports store a source fingerprint. After a successful import, later edits, deletion, malformed content, or unsafe replacement of that backup are ignored because SQLite is authoritative. The importer never edits or deletes the source JSON.

The first successful Google cutover for a ledger generation snapshots Players and both history worksheets into SQLite before importing any row. A crash resumes from that frozen snapshot, so one import cannot combine different live Sheet revisions. Sheet Silver becomes the current balance; history rows are retained for audit but are not replayed into Silver. Invalid, duplicate, or conflicting rows are quarantined in `migration_issues`. Mutating ledger commands are temporarily gated for a linked guild until this first cutover completes.

At Discord readiness, the bot first resumes pending configuration teardown and
registration Member-role assignments, then reconciles configuration panels,
tickets, compositions, reaction panels, and objective actions. Discord intents
are persisted before their side effect, and the persisted Discord resource ID
becomes authoritative as soon as it is available. Only the send-before-ID
window uses a short-lived, non-rendered checkpoint plus a Discord nonce. The
checkpoint is removed after the SQLite commit, and the nonce is never rendered
in Discord. On restart, bot-owned history is searched only when an ID was never
committed, and authoritative active artifacts are also swept for transient
checkpoints and visible markers left by older versions. Reconciliation then
completes or compensates the operation. Each workflow is isolated so one failure
does not starve the others. Persistent component views are registered again and
schedulers resume from local state. Ephemeral setup wizards and character
pickers intentionally do not survive because Discord interaction tokens cannot
be resumed safely.

Registration nickname updates are attempted only in the command request. They
are deliberately not persisted or retried by restart/runtime reconciliation;
only an incomplete configured Member-role assignment remains durable.

`/bot-remove` atomically archives the active ledger generation while removing the
live guild mapping. A later setup creates a new empty generation, including when
the same Albion guild is selected again. Archived registrations, balances,
histories, unfinished/dead-letter outbox events, and import evidence remain
available for audit but cannot leak into the new active setup.

## Optional Google synchronization

Google Sheets is an optional projection and Siphon calculation engine; it is not a general database.

Local-to-Google flow:

1. A command commits authoritative SQLite state and an outbox event atomically.
2. Balance and lootsplit commands directly rewrite the affected current Players
   rows by Discord ID after the local commit. This best-effort fast path does not
   consume or wait behind the outbox.
3. The background worker delivers and retries the complete FIFO player/history
   projection without importing Siphon.
4. Stable event IDs make retries idempotent.
5. Failed or rate-limited events remain pending with backoff; local commands do not roll back.

After repeated failures an event moves to a dead-letter table instead of
blocking newer operations forever. `/sync-status`, `/sync-retry`, and
`/sync-rebuild` expose bounded operator recovery. Completed delivery rows and
applied import snapshots are pruned after 30 days; immutable economy histories,
registrations, dead letters, and migration evidence remain retained.

The Players projection uses columns A-D for Discord ID, nickname, membership,
and numeric Silver; E remains the Sheet-calculated Siphon. Rows are located by
unique Discord ID. Older bot-owned registration-ID/revision columns are cleared
in place when their exact legacy headers are found, without shifting or touching
repurposed trailing columns. History worksheets receive an additional
`Realm Event ID` column. A newly linked physical Sheet is seeded from SQLite,
including immutable histories with stable event IDs. It is never treated as an
import source again after that ledger generation's first completed legacy cutover.

Google-to-local flow is restricted to the explicit `/sync-siphon` command; the
background worker never imports it. A row must have one unique Discord ID, active
membership, current Silver, and an integer Siphon. The command binds accepted
values to the player's current internal SQLite revision in one transaction, so
a concurrent or later balance/membership change invalidates the cached Siphon
without exposing revision metadata in Google. Missing, duplicated, invalid, or
Silver-mismatched rows have their older cached values cleared.

## Security and consistency rules

- Credential files are confined to `google_sheet_credentials/`, written with `0600`, and never followed through symlinks.
- Discord mentions are denied globally and enabled only for explicit trusted user IDs.
- Privileged and self-assignable roles may overlap when intentionally selected
  by an administrator; managed-role, hierarchy, and permission validation still applies.
- Objective notification roles are tracked by Discord role ID plus local ownership metadata and are not reused or deleted after drift.
- Albion and Google calls use bounded executors, request timeouts, and command cooldowns.
- Setup, update, removal, registration, membership, and credential linking coordinate through per-guild lifecycle generations.
- Registration and economy uniqueness is enforced by SQLite constraints and transactional idempotency keys.
- Every process-owned background worker is cancelled and awaited during bot shutdown.

## Product boundary

Registration verifies that an exact Albion character exists in the configured Albion guild. Albion's public API cannot prove that the Discord account owns that character. Officer approval or an in-game ownership challenge remains a separate product workflow.
