# Realm Protector Discord bot

Realm Protector is an operations bot for Albion Online guilds. It combines Discord onboarding and coordination with Albion API verification and a local SQLite member ledger. Its main jobs are character registration, guild-membership enforcement, application tickets, party compositions, objectives, reaction roles, lootsplit payouts, and balance history. Google Sheets is optional and is used only as a local-data projection and the calculation engine for Siphon.

The codebase follows the high-level layout: a small executable entry point and a feature package split into Discord presentation, domain rules, application services, and infrastructure adapters. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the dependency rules and file map.

## Invitation setup

1. In the Discord Developer Portal, enable the privileged **Server Members Intent**
   and **Message Content Intent** for the bot. Membership enforcement needs the
   former; `!create-comp` and party-thread messages need the latter.

2. Use the following least-privilege link to invite the bot: https://discord.com/oauth2/authorize?client_id=1473795901079421152&permissions=309640424562&integration_type=0&scope=bot%20applications.commands

   It grants the permissions used by the implemented workflows: Manage Server
   (UTC name timer), Kick Members (optional leave action), Manage Channels,
   Manage Roles, Manage Nicknames, Manage Messages, View/Send Messages, Embed
   Links, Attach Files, Read Message History, Add Reactions, and create/send in public threads.
   It does not request Ban Members, Mention Everyone, Moderate Members, event
   management, or poll permissions.

## Upgrading from v1.x to v2.0.0

Version 2.0.0 moves the source of truth from legacy JSON and Google Sheets to
local SQLite. Before upgrading:

1. Stop the v1 bot. Back up `.env`, `configs/`, `google_sheet_credentials/`, the
   linked Google Sheets, and any existing `data/realm_protector.sqlite3` file.
2. Install Python 3.12, 3.13, or 3.14 and create a new virtual environment. Do
   not reuse a Python 3.9 environment.
3. Install the v2 requirements, then run the migration explicitly if you want to
   review its JSON report before normal startup:

```bash
python scripts/migrate_to_sqlite.py
```

   Startup performs the same idempotent local migration automatically. Use
   `--skip-google` for an offline JSON-only pass.
4. Start the bot. For every linked Sheet, wait for the one-time cutover to
   complete and check `/sync-status` before using ledger mutation commands.

After cutover, SQLite is authoritative. Legacy JSON edits are ignored. Manual
changes to projected Google player/history fields are overwritten by local data;
only Siphon is imported on demand with `/sync-siphon`, using a unique Discord ID
and current Silver check. Restore the complete pre-upgrade backup before
attempting to roll back to v1.

## Local setup

1. Install Python 3.12, 3.13, or 3.14. Python 3.14 is the pinned deployment
   runtime in `.python-version` and is recommended; newer, untested interpreter
   releases are intentionally rejected by the package metadata.
2. Create and activate a virtual environment. Do not reuse an older Python 3.9
   environment—the interpreter version is fixed when the environment is created.

```bash
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip==26.2.1
```

   Confirm it reports Python 3.12-3.14 with `python --version`.
3. Install the hash-locked production dependency set (generated for Python 3.14):

```bash
python -m pip install --require-hashes -r requirements.lock
```

   `requirements.txt` contains the direct cross-version constraints used by CI
   on Python 3.12-3.14. Install `requirements-dev.txt` as well for local lint,
   type-check, audit, and coverage tools.

4. Copy `.env.example` to `.env` and set a Discord token:

   - `BOT_ENV=test` or `BOT_ENV=development` uses `DISCORD_TOKEN_TEST`.
   - `BOT_ENV=production` uses `DISCORD_TOKEN`.
   - With no `BOT_ENV`, `DISCORD_TOKEN` is used for backward-compatible production startup.

   Edit `resources/messages/startup_notification.txt` to change the message sent
   to configured bot-updates channels after startup. This copy is intentionally
   file-based and is not read from `.env`.

5. Run:

```bash
python main.py
```

6. In Discord (server admin):
	 - Run `/bot-setup` (required for most commands)
	 - Run `/bot-link-google-sheet` only if you need Siphon calculation or a Google mirror

## Security warning

- The authoritative database is `data/realm_protector.sqlite3`, restricted to the host account. Set `REALM_PROTECTOR_DATABASE_PATH` to override its location.
- Service-account credential payloads remain separate private JSON secret files; only their filename and Sheet mapping are stored in SQLite.
- The data and credentials directories are restricted to mode `0700`; the database and credential files use mode `0600`.
- Credential filenames are confined to `google_sheet_credentials/`; absolute paths, traversal, and escaping symlinks are rejected.
- Linking is transactional: if credential-link metadata cannot be saved, the new credential file is removed or the previous file is restored.
- The bot owner/operator has access to the database and credential secrets.
- Use a dedicated, least-privilege Google service account only for the Google Sheets link.
- Public Albion/Google-heavy commands use per-user/server cooldowns and bounded worker pools; Google requests have explicit connect/read timeouts.
- `/bot-remove` is coordinated with in-flight registration, membership, setup, update, and credential-link work so stale operations fail closed instead of recreating or mutating removed state.
- `/bot-remove` archives the server's current member ledger instead of deleting it. A later `/bot-setup` starts an empty ledger generation, even when it targets the same Albion guild, so old balances and registrations cannot silently become active again.
- Albion's public API can confirm that a character is in the guild, but cannot prove that the Discord user owns that character. Keep the automatically assigned Member role non-privileged and manually review registrations until an officer-approval or in-game challenge workflow is added.

## Development

Run the complete test suite without connecting to Discord:

```bash
python -m unittest discover -s tests -v
```

Run the same static gates as CI:

```bash
ruff check .
ruff format --check .
mypy src main.py scripts
pip-audit --strict --require-hashes --disable-pip -r requirements.lock
```

The test suite covers the preserved command surface, import-safe startup, SQLite
migrations, exact Albion identity matching, concurrent party claims, objective
role ownership, credential confinement, lifecycle races, mention allowlists,
Google synchronization, and transactional balance accounting.

## Server message reactions

- A human-authored server message containing the complete word `housri`, in any
  letter case, receives `resources/gif/8x4qbf.gif` as a reply.
- Direct messages, bot/webhook messages, and longer words that merely contain
  those letters are ignored. The reply never mentions or pings the author.

## Prefix commands

- `!create-comp <comp_message_id> <source_channel_id>`
- `!bal`
- `!lb`

## Slash commands

- `/bot-setup`
- `/bot-link-google-sheet`
- `/bot-remove`
- `/tickets-setup`
- `/role-reaction-setup`
- `/set-objective-panel`
- `/update-config`
- `/register`
- `/force-register`
- `/get-participants`
- `/lootsplit`
- `/bal`
- `/get-negative-siphon`
- `/bal-add`
- `/bal-remove`
- `/clear`
- `/add-utc-timer`
- `/sync-status`
- `/sync-retry`
- `/sync-rebuild`
- `/sync-siphon`

## Permissions model

- Admin-only:
	- `/bot-setup`, `/bot-link-google-sheet`, `/tickets-setup`, `/role-reaction-setup`, `/set-objective-panel`, `/update-config`, `/bot-remove`, `/force-register`, `/clear`, `/add-utc-timer`, `/sync-status`, `/sync-retry`, `/sync-rebuild`
- Economy operations (`/lootsplit`, `/get-negative-siphon`, `/sync-siphon`, `/bal-add`, `/bal-remove`):
	- Allowed for Admins OR members with configured Economy Manager role(s)
- Comp officer actions (`!create-comp`, forced sign-up/sign-out in party threads):
	- Allowed for Admins OR members with configured Caller role(s)
- Objective add/remove actions:
	- Allowed for Admins OR members with configured Caller role(s)
- `/register`, `/bal`, `!bal`, `!lb`, `/get-participants`, and normal thread self sign-up/sign-out are available without admin requirement.

Caller, Economy Manager, and ticket-management roles may also be configured as
the Member role or a reaction role. This intentionally allows users to acquire
the associated privileged bot actions through that self-assignment workflow.

## Bot setup and configuration

### `/bot-setup`

Configures per-server values:

- Guild name
- Caller role(s) selection
- Economy Manager role(s) selection
- Member role selection
- Bot updates channel selection
- Leave guild action (Kick from server / Remove all roles / Do nothing)

After setup, bot posts/updates a persistent **Bot configuration** message in the channel.

### `/bot-link-google-sheet`

Optionally links service account JSON and a Sheet mapping. Registration,
membership, Silver, lootsplits, histories, and all panels work without this
link. Siphon requires it because the formula remains in Google Sheets.

The first successful link for a ledger generation imports Players and both
history worksheets into SQLite once. Before applying anything, the bot stores a
complete immutable Sheet snapshot in SQLite; if the process stops midway, the
next startup resumes from that exact snapshot instead of mixing rows from two
different Sheet revisions. Existing Sheet Silver becomes the starting local
balance; imported history is audit-only and is not added to that balance. After
cutover, SQLite always wins. Local changes are mirrored through a retryable
outbox, Siphon flows back only when `/sync-siphon` is run, and later relinks seed
players and immutable history from SQLite using stable event IDs.

Link fields:

- Credentials JSON (full service account JSON text)
- Google Sheet name (default: refers to guild name that was set up in `/bot-setup`)
- Players worksheet name (default: `Players`)
- Lootsplit History worksheet name (default: `Lootsplit History`)
- Balance History worksheet name (default: `Balance History`)

After linking, the same persistent configuration message is updated and reports
whether the one-time SQLite cutover completed or is waiting for an automatic retry.

Admins can inspect and recover this optional projection without changing local
business data:

- `/sync-status` reports link/cutover state, queue and dead-letter counts, the
  latest projection result, and Siphon-cache coverage.
- `/sync-retry` requeues a bounded batch of quarantined outbox events and flushes
  it to Google.
- `/sync-rebuild` reconstructs bot-owned player and history fields from SQLite.
  It is refused until the initial legacy cutover is complete and never overwrites
  the Sheet-owned Siphon column.
- `/sync-siphon` is available to Admins and configured Economy Managers. It
  explicitly replaces the current local Siphon cache from column E without
  changing Silver or any other authoritative SQLite field.

Your linked Google Sheet is required to have 3 worksheets with the **EXACT** naming you provided in `/bot-link-google-sheet`:

- **Players** worksheet (values are examples):

| Discord ID | Albion Nickname | Is In Guild | Silver | Siphon |
|------------|-----------------|-------------|--------|--------|
| 1234567890 | Nickname | YES | 0 | 0 |

- **Lootsplit History** worksheet:

| Battleboard ID | Date | Officer | Content name | Caller | Participant | Lootsplit | Realm Event ID |
|----------------|------|---------|--------------|--------|-------------|-----------|----------------|
| 1234567890 | 03/24/26 17:44 UTC | Officer name | Terry defence | Caller name | Participant name | 2500000 | generated-id |

- **Balance History** worksheet:

| Date | Reason | Officer | Nickname | Amount | Realm Event ID |
|------|--------|---------|----------|--------|----------------|
| 03/24/26 17:44 UTC | Payout | Officer name | Player name | -2500000 | generated-id |

Header matching is case-sensitive. The bot creates history event-ID headers when
they are blank. Keep the Siphon formula entirely in column E—prefer one
`ARRAYFORMULA` managed by the Sheet—and do not make A-D operator-editable. The
bot locates each player by a unique Discord ID and accepts Siphon only when the
Sheet Silver still matches SQLite. `/sync-siphon` performs this import manually;
the background worker only delivers and retries local-to-Google projections.
Legacy `Realm Registration ID` and `Realm Revision` columns are cleared when the
bot recognizes their exact old headers; unrelated trailing columns are preserved.

- You can make a copy of the following Google Sheet: https://docs.google.com/spreadsheets/d/1N9YYq0tNboJsG0n9fvTngG0JfXxO2zwaKAKMPEWjBuc

### `/bot-remove`

Removing the bot setup unlinks live configuration and archives the active SQLite
ledger generation; it does not erase registrations, balances, history, pending
sync records, or migration evidence. Running `/bot-setup` again creates a new,
empty active generation. Archived generations remain in the database for audit
and recovery but are never read by live panels or commands.

### `/update-config`

Interactive update panel in chat:

1. Bot posts an interactive panel in chat
2. Admin selects the configuration that needs to be updated
3. Admin enters or selects a new value for the chosen configuration
4. After confirmation, bot updates SQLite and refreshes the persistent configuration panel

Supported fields:

1. Guild name
2. Caller role(s)
3. Economy Manager role(s)
4. Member role
5. Leave guild action
6. Credentials file
7. Google Sheet name
8. Players Worksheet name
9. Lootsplit History Worksheet name
10. Balance History Worksheet name

Safety checks:

- Guild name update is blocked if already used by another server.
- Credentials file update requires the file to exist in `google_sheet_credentials/`.

## Guild membership tracking

The bot runs a background audit every **3 minutes** to detect players who left the configured Albion guild.

- The audit reads active registrations from SQLite and does not require Google.
- A recent registration receives a 10-minute grace period, and a departure must
  be confirmed by three Albion responses before local state changes.
- Before committing a departure, the audit rechecks the player's local revision
  so stale API work cannot overwrite a newer registration or balance update.
- A confirmed departure only changes `Is In Guild` to `NO`; nickname, Albion ID,
  Silver, all-time earnings, and histories remain stored in SQLite.
- If Google is linked, the resulting local membership status is projected later as `Is In Guild = NO`.
- After the local state change, the configured **Leave guild action** is applied:
	- **Kick from server**
	- **Remove all roles** (all non-managed roles the bot is allowed to remove)
	- **Do nothing**

## Ticket system

### `/tickets-setup`

Admin command used to configure guild application ticket panels.

Main setup entry has 2 buttons:

- `Create Panel`
- `Manage Panels`

### Create Panel flow

Panel creation uses 7 steps:

1. Set panel name
2. Select management team role(s)
3. Select open ticket category
4. Select ticket archive channel
5. Select panel destination channel
6. Set panel message and ticket opening message
7. Review summary and finish

The created panel contains an `Open Ticket` button.

The message step opens a modal where admin can customize:

- The panel embed message shown before opening a ticket
- The opening message shown inside newly created tickets

### Ticket behavior

- Clicking `Open Ticket` opens a modal where the user must enter their **Albion character nickname**.
- The bot shows the first three Albion search matches with guild, Kill Fame,
  Death Fame, Fame Ratio, and PvE Fame information.
- The user selects character **1**, **2**, or **3**, or cancels. Unavailable
  result buttons are disabled.
- After selection, the bot creates a new text channel under the selected category
  using the selected character's nickname and stats.
- Ticket channels are named `open-character-nickname`; no ticket number is added.
- Only the applicant and selected management team can view the ticket.
- The ticket contains a `Close Ticket` button.
- When management team closes the ticket:
	- Bot sends an archive entry to the configured ticket archive channel (message content: character nickname)
	- Bot creates a thread under the archive message and forwards the ticket messages (including links and attachment URLs)
	- Bot deletes the ticket channel
- Existing legacy panels that store a closed-ticket category remain compatible:
  they rename, move, and lock the ticket channel instead of creating a transcript
  thread. Create a replacement panel to adopt the archive-channel workflow;
  already-open legacy tickets remain closable.

### Manage Panels

Manage Panels lets admin:

- View configured panels
- Send a selected panel again to its configured destination channel
- Delete a selected panel

## Role reaction panels

### `/role-reaction-setup`

Admin command used to configure **role reaction panels**.

The setup message contains 2 buttons:

- `Create new panel`
- `Manage panels`

### Create new panel flow

Panel creation uses 5 steps:

1. Set panel name
2. Set panel message
3. Add emoji → role mappings (up to 6)
4. Select destination channel
5. Preview summary and confirm

When confirmed, the bot sends an embed to the chosen destination channel and adds the configured reactions.

### Role reaction behavior

- When a user reacts to a configured emoji, the bot adds the associated role.
- When a user removes the reaction, the bot removes the associated role.
- Only Unicode emojis are supported (you can also input a shortcode like `:gear:` during setup).

Required bot permissions:

- In the destination channel: **View Channel**, **Send Messages**, **Embed Links**, **Add Reactions**
- In the server: **Manage Roles** (and the bot role must be above the roles it needs to grant)

### Manage panels

Manage panels lets admin:

- Resend a selected panel to its configured destination channel (updates stored message reference)
- Delete a selected panel (also attempts to delete the last sent panel message)

## Objectives panel

### `/set-objective-panel`

Admin command that posts or updates a persistent **Objectives panel** in the current channel.

- Requires the server to be configured first via `/bot-setup`.
- If a panel already exists in another channel, the bot moves it by deleting the old message (or editing it with a “moved” notice if it cannot delete).

## UTC timer in server name

### `/add-utc-timer`

Admin command that appends the current UTC time to the server name in `Server Name [HH:MM]` format.

- Requires the server to be configured first via `/bot-setup`.
- The bot updates the server name every five minutes.
- The original server name is stored and reused as the base, so the UTC suffix is always appended to the clean name.
- Running the command again reuses the stored base name and refreshes the current UTC suffix.
- The bot needs the `Manage Server` permission to rename the guild.

### Adding objectives

The Objectives panel contains an `Add Objective` button.

Only Administrators and members with a configured Caller role can add or remove
objectives.

When clicked, it opens an ephemeral wizard with 3 objective types:

- **Vortex**:
1. Select rarity (Common / Uncommon / Epic / Legendary)  
2. Set pop time (UTC, `HH:MM`)
3. Set map name
4. Notify before pop (5-60 minutes)
5. Confirm

- **Core**:
1. Select rarity (Common / Uncommon / Epic / Legendary)
2. Set pop time (UTC, `HH:MM`)
3. Set map name
4. Notify before pop (5-60 minutes)
5. Confirm
- **Node**:
1. Select node type (Wood / Hide / Ore / Fiber)
2. Select tier (4.4 / 5.4 / 6.4 / 7.4 / 8.4)
3. Set pop time (UTC, `HH:MM`)
4. Set map name
5. Notify before pop (5-60 minutes)
6. Confirm

After confirmation, the bot posts the objective as a separate message in the same channel as the objectives panel.

Objective message buttons:

- `Notify Me`: assigns a per-objective notification role to the clicker.
- `Remove Objective`: removes the objective early (Admins and configured Caller role(s)).

Objective notifications:

- When an objective is posted, the bot creates a non-mentionable role such as `Vortex-Epic-12:36`. Its Discord role ID and local ownership metadata keep objectives with the same label independent.
- At the chosen lead time, the bot sends one message mentioning the individual subscribers. The role remains non-mentionable so ordinary members cannot use it to ping everyone who opted in.
- Each objective accepts up to 75 subscribers so the trusted user-mention notification remains below Discord's message-size limit.
- Newly created notification roles are recorded by Discord role ID with local bot-ownership metadata. If an admin renames, elevates, or reuses one, the bot refuses to assign or delete it and logs the drift for operator review.
- When the objective pops, the bot deletes the notification role and the ping message.

Required permissions for notifications:

- **Manage Roles** (bot role must be above the created roles) for role creation and assignment.

### Objective lifecycle

- Objectives automatically “expire” after they pop: once the pop time is reached, the objective message is marked as popped and then removed ~60 seconds later.
- Manual removal is available via the `Remove Objective` button for Admins and members with configured Caller role(s).

## Registration and balances

### `/register <character_name>`

- Shows an ephemeral **Select your character** panel visible only to the command
  user, with the first three valid Albion matches and their guild, Kill Fame,
  Death Fame, Fame Ratio, and PvE Fame.
- Provides **1**, **2**, **3**, and **Cancel** buttons; unavailable result buttons
  are disabled and only the command user can select one.
- Revalidates the selected character's exact nickname and configured Albion guild
  membership before changing local or Discord state.
- Creates a unique SQLite registration with Discord ID, Albion ID/nickname,
  in-guild state, revision, Silver starting at `0`, and all-time earnings
  starting at `0`.
- Attempts to update the Discord nickname to the Albion nickname once. A failed
  nickname change is reported but is not queued for a later retry.
- Adds the configured Member role. A failed role assignment remains queued for
  recovery after restart and by the runtime reconciliation loop.
- If Google is linked, immediately adds or refreshes the registered Discord ID
  in the Players worksheet after SQLite commits. Google downtime does not roll
  back the local registration, and new/reactivated registrations remain queued
  for automatic projection retry.
- Character search, selection, and progress remain ephemeral. After selection,
  the registration result is posted publicly in the channel.

### `/force-register <member>`

- Admin-only recovery command for an existing registered Discord member.
- Rechecks the stored Albion character by stable Albion ID, or by exact nickname
  for a legacy registration, and retries briefly for Albion propagation delays.
- Changes the registration back to `Is In Guild = YES` only after the Albion API
  confirms membership in the configured guild.
- Preserves Silver, all-time earnings, histories, and existing registration data,
  then attempts the configured Member role and Discord nickname repairs. Only a
  failed Member-role repair is queued for a later retry.
- If Google is linked, immediately refreshes the member's authoritative Players
  row after the local registration is reactivated.

### `/bal [member]` and `!bal`

- Reads authoritative Silver and the last valid cached Siphon snapshot from SQLite.
- Displays all-time earnings: the cumulative positive Silver credited by manual
  additions and lootsplits. It appears as a full-width row below the
  **Balance**/**Siphon** row and above **Raw balance**; balance removals never
  reduce this value.
- Existing positive Balance History imported during the one-time Google cutover
  is counted once; its duplicate Lootsplit History audit rows are not replayed.
- `/bal` can inspect an optional member; `!bal` has no arguments and checks the
  invoking user's own balance. All variants share the same balance view and show
  the displayed player's current Silver leaderboard position in the footer.
- Without Google, Silver remains available and Siphon is shown as unavailable.
- A Siphon invalidated by a newer local change is shown as pending until an
  Economy Manager or Admin runs `/sync-siphon`; elapsed time alone does not
  invalidate an otherwise current value.

### `!lb`

- Posts the public Silver leaderboard from authoritative SQLite data.
- Sorts all registered players by current Silver from highest to lowest, with a
  deterministic nickname/Discord-ID order for ties.
- Shows continuous ranking numbers, 10 players per page, a `Page X/Y` counter,
  and persistent **Previous**
  and **Next** buttons that continue working after a bot restart.

### `/bal-add` and `/bal-remove`

- Work by Discord member mention.
- Validate amount as an integer greater than `0`.
- Update Silver and immutable Balance History atomically in SQLite:
	- Date, Reason, Officer, Nickname, Amount
- Positive `/bal-add` credits also increase all-time earnings; `/bal-remove`
  never decreases all-time earnings.
- When Google is linked, immediately attempt to write the affected player's
  current A-D fields, including committed Silver as a numeric cell. The full
  player/history event stays queued for idempotent automatic delivery, and a
  Google failure never rolls back SQLite.
- Defaults:
	- `/bal-add` reason: `Manual`
	- `/bal-remove` reason: `Payout`

## Lootsplit flow

### `/lootsplit`

Inputs:

- `battle_ids` (CSV)
- `content_name`
- `caller`
- `participants` (CSV)
- `lootsplit_amount`
- `officer` (optional; defaults to the member running the command)

Behavior:

- Credits all found participants, the lootsplit record, and immutable balance
  history in one SQLite transaction.
- Commits every matched credit together and reports any missing participant names.
- When a Sheet is linked, immediately rewrites every credited Players row after
  SQLite commits. Lootsplit and balance histories remain in the durable outbox
  for idempotent delivery and retry.

Google projection failures use retry-with-backoff and never change the completed local result.

## Party comp thread behavior

### `!create-comp <comp_message_id> <source_channel_id>`

Before using this command, it is necessary to create a comp message. A Comp message can consist of multiple parties and each party **MUST** start with the **Party** word.  

Single party comp message example:

```
Party 1
1. Tank
2. Support
3. DPS
4. Heal
```

Multiple parties comp message example:

```
Party 1
1. Tank
2. Support
3. DPS
4. Heal

Party 2
1. Tank
2. Support
3. DPS
4. Heal

Party Battle Mounts
1. Behemoth
2. Chariot
3. Balista
4. Venom Basilisk
```

In `Party ... thread` threads:

- `1` → sign up to role `1`
- `-` → sign out self
- `-1` → force sign out role `1` (caller/admin only)
- `@User 1` → force sign up mentioned user to role `1` (caller/admin only)

Each member can occupy only one role per party thread. They must sign out before
choosing another role; caller-forced signups follow the same rule.
Party-message edits process only the assigned members' user mentions, so the
composition is highlighted for them without allowing role or `@everyone` pings.

## Local storage and migration

- `data/realm_protector.sqlite3` is the authoritative database. Its WAL/SHM
  companion files may exist while the bot is running.
- `google_sheet_credentials/*_credentials.json` contains optional service-account
  secrets. These remain separate from SQLite and must be backed up securely.
- The old `configs/*.json` and `google_sheet_credentials/credentials_links.json`
  files are one-time migration inputs only. Successful imports are fingerprinted;
  later changes are ignored and the files are left untouched as recovery backups.

Startup runs local schema upgrades and the JSON migration automatically before
the Discord client is created. Discord then connects, and the background Google
worker immediately attempts the one-time import for linked Sheets. This keeps a
temporary Google outage from preventing the bot from logging in. Local ledger
mutations remain gated only for a linked generation whose initial Sheet cutover
has not completed.

To run it explicitly and receive a JSON report:

```bash
python scripts/migrate_to_sqlite.py
```

Use `--skip-google` for an offline JSON-only pass, `--database PATH` for a custom
database, and `--project-root PATH` when invoking the script from elsewhere. If
`--database` is omitted, the script loads the project `.env` and honors
`REALM_PROTECTOR_DATABASE_PATH`, exactly like normal bot startup. The command is
idempotent and returns a non-zero status when a required local import or requested
Google cutover is still failing.

Completed projection-delivery records and applied immutable Sheet snapshots are
derived recovery artifacts and are pruned in bounded batches after 30 days.
Registrations, balances, lootsplit/balance histories, dead letters, and migration
issues are not removed by this maintenance job.

Published ticket, reaction-role, and objective panels, ticket close/archive
work, active party compositions, objective creation/notification work, timers,
and schedulers are restored or reconciled from SQLite after a restart. Temporary
ephemeral setup and character-selection dialogs cannot be resumed because their
Discord interaction tokens expire; the user can safely run those dialogs again.
Completed Discord artifacts do not retain checkpoint text, footer IDs, or role-name
suffixes. A short-lived non-rendered token covers only the send-before-ID commit
window; restart reconciliation removes it and any visible markers from older releases.

## Notes

- Enable **Developer Mode** in Discord to copy message/channel IDs for `!create-comp`.
- If you rename worksheet tabs in Google Sheets, update mapping via `/update-config` or relink with `/bot-link-google-sheet`.

## Recommendations from the bot author:

- Create a separate category for the bot setup and configuration.
- Create separate channels for each setup:
	- `#bot-updates`
  	- `#bot-setup`
	- `#tickets-setup`
  	- `#role-reacts-setup`
  	- `#comp-storage`

### `#bot-updates`

- Channel with updates where the message will be sent when the hosting server is restarted, and informs about new bot updates if there are any.

### `#bot-setup`

- Channel where `/bot-setup` and `/bot-link-google-sheet` commands should be used. Bot configuration persistent panel will be sent here as well.

### `#tickets-setup`

- Channel where the `/tickets-setup` command should be used. An interactive panel to manage and create new ticket panels will be sent here.

### `#role-reacts-setup`

- Channel where the `/role-reaction-setup` command should be used. The setup wizard and panel management messages are sent here.

### `#comp-storage`

- Channel where comp messages will be stored. Send `!create-comp 1234567890 0987654321` to use the quick-access comp creation command.
