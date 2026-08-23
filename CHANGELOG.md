# Changelog

All notable changes to this project will be documented in this file.

This project aims to follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [v2.0.0] - 2026-08-23

Realm Protector 2.0.0 is a major local-first reliability release. SQLite is now
the authoritative source for bot data, stateful Discord workflows recover after
restarts, and registration, tickets, party composition, and economy workflows
have been expanded and hardened.

### Upgrade notes

- **Breaking:** Python 3.12, 3.13, or 3.14 is required. Recreate virtual
  environments made with Python 3.9 or another unsupported interpreter.
- Stop the v1 bot before upgrading and back up `.env`, `configs/`,
  `google_sheet_credentials/`, the linked Google Sheets, and any existing
  `data/realm_protector.sqlite3` database.
- The first v2 startup creates/upgrades SQLite and imports legacy JSON
  automatically. `python scripts/migrate_to_sqlite.py` performs the same
  idempotent migration explicitly and returns a machine-readable report.
- A linked Google Sheet is imported once from an immutable snapshot. Ledger
  mutations remain temporarily gated until that cutover succeeds. Afterward,
  SQLite wins: manual edits to projected player/history fields are overwritten,
  and only revision-matched Siphon calculations flow back into local storage.
- Legacy JSON inputs are retained as untouched backups but are ignored after a
  successful import. Restore the complete pre-upgrade backup before attempting
  a v1 rollback.

### Added

- Added authoritative SQLite persistence for guild configuration, ledger
  generations, registrations, membership, Silver, all-time earnings, histories,
  panels, objectives, tickets, compositions, timers, and active runtime actions.
- Added automatic, fingerprinted legacy JSON migration and immutable Google
  Sheet cutover, plus the standalone `scripts/migrate_to_sqlite.py` migration
  and reporting command. Invalid or conflicting source rows are quarantined.
- Added durable Discord reconciliation for panels, open tickets, ticket
  close/archive work, party threads, reaction roles, objectives, notifications,
  configuration removal, and failed Member-role assignment. Persistent views
  and schedulers are restored after restart.
- Added a durable Google projection outbox, dead-letter recovery, revision-safe
  Siphon caching, full rebuild/retry support, and bounded 30-day cleanup of
  completed delivery and snapshot records.
- Added admin-only `/sync-status`, `/sync-retry`, and `/sync-rebuild` commands for
  inspecting and repairing the optional Google projection.
- Added ledger generations. `/bot-remove` archives the active ledger without
  deleting its data, and a later setup receives a new empty generation.
- Added admin-only `/force-register @User`. It reverifies the stored Albion
  identity, reactivates only a confirmed guild member, and preserves all economy
  and history data while attempting Discord nickname and Member-role repair.
- Added cumulative all-time earnings. Manual positive credits and lootsplit
  payouts increase it atomically; deductions never reduce it. Positive legacy
  Balance History is backfilled once without replaying duplicate lootsplit rows.
- Added the argument-free `!bal` command and the paginated `!lb` Silver
  leaderboard with deterministic numbering, 10 entries per page, restart-safe
  Previous/Next controls, and a page counter. `!bal`, `/bal`, and `/bal @User`
  share one balance panel and show the displayed player's leaderboard position.
- Added three-result Albion character pickers to tickets and `/register`, with
  full character summaries and `1`/`2`/`3`/`Cancel` controls. Registration
  selection is visible only to its invoker while the final result remains public.
- Added a case-insensitive server-wide `housri` whole-word GIF response that
  ignores DMs, bots, and webhooks.
- Added Python 3.12-3.14 CI for package installation, Ruff, mypy, coverage,
  offline tests, hash-locked dependencies, and vulnerability auditing. Added an
  early unsupported-interpreter guard, architecture documentation, and extensive
  regression coverage.

### Changed

- Made Google Sheets optional except for Siphon calculation. Local commits are
  authoritative and project asynchronously; later manual edits to mirrored
  fields never overwrite SQLite.
- Reorganized the bot under `src/realm_protector/` into Discord presentation,
  domain, application-service, and infrastructure layers. All 16 existing v1
  slash commands were retained, with `/force-register` and three `/sync-*`
  commands added in this release.
- Made `main.py` import-safe, added explicit production/test token selection,
  pinned runtime dependencies, moved runtime messages and media under
  `resources/`, and hardened graceful shutdown of background workers.
- Made `resources/messages/startup_notification.txt` the configurable source for
  restart notifications.
- Hardened membership departure checks with a 10-minute recent-registration
  grace period, three consecutive non-membership confirmations, and a final
  local revision check. Confirmed departures retain the full player record,
  change only membership to `NO`, and then apply the configured leave action.
- Allowed self-assignable Member and reaction roles to include Discord's
  **Mention Everyone** and **Set Voice Channel Status** permissions. Removed the
  privileged/self-assignable role-overlap restriction while retaining managed
  role, hierarchy, and all other fail-closed permission checks.
- Changed balance panels to show Balance and Siphon side by side, followed by
  full-width All-time earnings and Raw balance rows.
- Made edited party compositions highlight all assigned users while denying
  role, `@everyone`, and reply-author pings.
- Registration now attempts a failed Discord nickname update only during the
  command; nickname failures are not persisted for restart/runtime retry. Failed
  configured Member-role assignments remain recoverable.
- Moved blocking Albion and Google work to bounded executors, configured explicit
  Google timeouts, added API-heavy command cooldowns, and serialized lifecycle and
  mutation paths so stale work cannot outlive setup replacement or removal.
- Reduced UTC server-name refreshes to five-minute intervals while preserving
  the displayed `HH:MM` format.

### Removed

- Removed legacy JSON and direct Google worksheet access as runtime sources of
  truth. Successful legacy files remain available only as migration backups.
- Removed ticket sequence numbers from channel names, metadata, embeds, and
  configuration. Ticket identity and display now use the selected Albion
  character nickname.
- Removed visible internal recovery IDs and checkpoint text from completed
  configuration, ticket, archive, reaction-role, composition, and objective
  artifacts. Crash recovery uses SQLite IDs and short-lived non-rendered tokens.
- Removed the `.env` restart-message override and obsolete JSON, worksheet,
  timer-channel, and compatibility APIs.

### Fixed

- Fixed party-thread auto signup by resolving Discord's thread-starter wrapper
  back to the editable parent message. Self and caller-forced signups now enforce
  one role per member in each party and normalize both Discord mention formats.
- Fixed exact Albion nickname selection, stable-ID registration verification,
  delayed guild-membership propagation, and false departure processing after a
  successful registration.
- Fixed balance-history clamping, duplicate lootsplit participants, all-time
  earnings backfill, and stale Siphon acceptance.
- Fixed malformed or mismatched worksheet schemas, interrupted initial Sheet
  imports, duplicate projection delivery, and linked-Sheet guild mismatches.
- Fixed role-ID authorization and automatic role assignment while continuing to
  reject Discord-managed or otherwise unsafe roles.
- Fixed ticket open/close races, duplicate tickets, archive resumption, transcript
  checkpoint leakage, and cleanup of legacy ticket markers.
- Fixed objective role ownership, notification cleanup, retry behavior, and
  reconciliation of stale objective artifacts.
- Prevented stale membership audits from applying leave actions after teardown,
  stale Google-link wizards from recreating removed secrets, and public ticket
  character lookups from running when an open ticket already exists.
- Prevented an in-flight UTC scheduler tick from restoring the timer suffix after
  `/bot-remove`; enabling the timer now requires an active main setup.
- Serialized configuration-panel refreshes with setup/removal so a concurrent
  `/bot-remove` cannot leave behind a newly posted orphan panel.
- Objective notification roles are tracked by Discord role ID plus local
  ownership metadata. Verified legacy hash-suffixed roles are renamed cleanly;
  renamed, elevated, shared, or otherwise repurposed roles are detached and left
  untouched instead of being assigned or deleted.

### Security

- Confined service-account files to the credentials directory, isolated new
  files by Discord guild ID, rejected traversal and symlink escapes, used atomic
  owner-only writes, rolled credential files back when link metadata fails, and
  hardened existing local secret/configuration modes.
- Added bounds for battle IDs, monetary inputs, content names, and balance
  reasons; restricted ticket archives and validated role hierarchy/permissions.
- Denied Discord mentions by default and allowlisted only exact intended user
  IDs, including messages containing officer- or member-supplied text.

## [v1.1.5] - 2026-04-09

### Changed

- `/add-utc-timer` now appends the current UTC time to the server name instead of creating a voice channel.
- `/add-utc-timer` now refreshes the UTC suffix in the server name every minute.

## [v1.1.4] - 2026-04-09

### Added

- New admin command `/add-utc-timer` that creates a voice channel displaying the current UTC time.

## [v1.1.3] - 2026-04-04

### Fixed

- Economy command responses that exceed Discord's message length limit are now split into multiple follow-up messages instead of failing.
- `/lootsplit` and `/get-negative-siphon` no longer error when the response content grows beyond 2000 characters.

## [v1.1.2] - 2026-04-03

### Changed

- Updated check interval for leave guild action: 300 seconds -> 180 seconds.

## [v1.1.1] - 2026-04-03

### Added

- Economy command:
  - Added `/get-negative-siphon` to mention all users with negative Siphon balance, ordered from most negative to least negative.

### Changed

- `/get-participants` now sorts participant names case-insensitively so names are grouped regardless of uppercase/lowercase differences.

## [v1.1.0] - 2026-04-03

### Changed

- Players worksheet schema now includes a fifth `Siphon` column alongside `Silver`.
- `/bal` panel improvements:
  - now shows the member's Discord avatar in the embed.
  - now shows a `Siphon` field read from the Players worksheet.
- `/lootsplit` command UX:
  - `Officer` and `Caller` now use Discord member selectors instead of manual nickname text.
  - selected `Officer` and `Caller` are resolved to their registered Players-sheet nickname when available, with Discord display name as fallback.
  - `Officer` is now optional and defaults to the member who runs the command.

## [v1.0.1] - 2026-04-02

### Changed

- Economy command UX:
  - `/bal` now supports checking another member with `/bal @User` instead of manual nickname entry.
  - `/bal` now responds with a balance panel that shows the requested member inside the panel and includes both formatted and raw balance values.
  - `/bal-add` and `/bal-remove` now respond with balance update panels instead of plain text messages.
  - Balance update panels now include the action summary plus `Reason`, `Old balance`, and `New balance` fields.

## [v1.0.0] - 2026-03-31

### Added

- Objectives:
  - New objective type: **Core** (rarities and wizard steps aligned with Vortex).
  - Objective notifications:
    - Wizard step to select **Notify before pop** (5–60 minutes).
    - Per-objective notification role + `Notify Me` button to opt-in.
    - One-time pre-pop ping, and automatic cleanup of the notification role and ping message when the objective pops.
- New slash commands:
  - `/get-participants` (battle participation lookup)
  - `/bal` (balance lookup)
  - `/clear` (admin-only message purge)

### Changed

- Command UX: migrated legacy prefix commands to slash commands (kept `!create-comp` as prefix-only).
- Albion API usage: standardized nickname lookup on `get_player_by_nickname` in features that previously used exact-search.
- Registration reliability: added retry behavior to reduce false negatives from stale Albion API `GuildName` responses.

### Fixed

- Objectives rarity display formatting.

## [v0.1.0] - 2026-03-24

### Added

- Discord bot core with per-server setup and configuration:
  - `/bot-setup` to configure guild name + roles (caller/economy/member), bot updates channel, and leave-guild action, then post a persistent configuration panel.
  - `/update-config` interactive config update panel for setup values (including leave-guild action).
- Google Sheets integration:
  - `/bot-link-google-sheet` to store service account credentials locally and link a sheet + worksheet names.
  - Lootsplit logging and balance history logging to the configured worksheets.
- Guild membership tracking:
  - Background audit every 5 minutes to detect when a player leaves the configured Albion guild.
  - Configurable action: kick from server, remove all roles, or do nothing.
  - Enforcement runs even if Google Sheets is not linked (role-based audit fallback).
- Ticket system:
  - `/tickets-setup` wizard to create/manage ticket panels for guild applications.
  - Ticket channels with open/close workflow and permission gating.
- Registration and economy commands:
  - `/register` to validate Albion character and write to Players worksheet.
  - `!bal`, `/bal-add`, `/bal-remove` for balance reads and manual adjustments.
  - `/lootsplit` to distribute lootsplit payouts and append history rows.
- Party comp tooling:
  - `!create-comp` and party thread sign-up/sign-out behavior.
- Role reaction panels:
  - `/role-reaction-setup` wizard to create panels that grant/remove roles based on reaction add/remove.
- Objectives panel:
  - `/set-objective-panel` to post/update a persistent objectives panel.
  - Objective wizard to post Vortex/Node objectives with automatic expiry after pop.

### Changed

- Players worksheet schema now has 4 columns: Discord ID, Albion Nickname, Is In Guild (YES/NO), Silver.
  - `/register` writes `Is In Guild=YES`.
  - Balance and lootsplit operations read/write Silver from column D.

### Security

- Service account credentials and server configuration are stored as local JSON files on the machine hosting the bot. Treat the host as sensitive.

[Unreleased]: https://github.com/vladyslav-babych/albion-online-sign-up-bot/compare/v2.0.0...HEAD
[v2.0.0]: https://github.com/vladyslav-babych/albion-online-sign-up-bot/compare/v1.1.5...v2.0.0
[v1.1.5]: https://github.com/vladyslav-babych/albion-online-sign-up-bot/releases/tag/v1.1.5
[v1.1.4]: https://github.com/vladyslav-babych/albion-online-sign-up-bot/releases/tag/v1.1.4
[v1.1.3]: https://github.com/vladyslav-babych/albion-online-sign-up-bot/releases/tag/v1.1.3
[v1.1.2]: https://github.com/vladyslav-babych/albion-online-sign-up-bot/releases/tag/v1.1.2
[v1.1.1]: https://github.com/vladyslav-babych/albion-online-sign-up-bot/releases/tag/v1.1.1
[v1.1.0]: https://github.com/vladyslav-babych/albion-online-sign-up-bot/releases/tag/v1.1.0
[v1.0.1]: https://github.com/vladyslav-babych/albion-online-sign-up-bot/releases/tag/v1.0.1
[v1.0.0]: https://github.com/vladyslav-babych/albion-online-sign-up-bot/releases/tag/v1.0.0
[v0.1.0]: https://github.com/vladyslav-babych/albion-online-sign-up-bot/releases/tag/v0.1.0
