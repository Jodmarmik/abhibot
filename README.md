<!-- coderabbit-ai-generated:v1 -->

# Telegram Auto Filter Bot

An asynchronous Python worker for searching and distributing files from channels you control or have permission to distribute. Uses **Python 3.12–3.14**, **Telethon 1.45**, **python-telegram-bot 22.8**, and **asyncpg 0.32**. Development and integration checks were run on Python 3.12.14 and PostgreSQL 17. Use Python 3.12 for deployment.

## Repository transition

This repository originally contained a Pyrogram voice-chat music bot with MongoDB. The new default worker is `python bot.py`. The original `AnonXMusic/`, `config.py`, language files, and music database code remain. MongoDB data is never migrated, reset, or read by the filter worker. Its PostgreSQL objects have an `af_` prefix.

Legacy music dependencies are preserved in `requirements-legacy.txt`; run them in a separate Python environment following the original music instructions in `.github/README.md`. `bash start` remains the legacy music entry point, with session deletion and system IPv6 changes removed. The legacy `setup` installer is **not** the filter bot installer; it changes system packages and rewrites `.env`. Do not run it for this application. Existing music deployments must explicitly retain their legacy start/build commands when upgrading.

Populated credential and owner defaults were removed from `app.json`. If those values or other historical credentials were real, rotate them: this patch does not rewrite Git history. Review legacy cookies/configuration before publishing a fork.

## Features

- Live searches of explicitly authorized source channels, plus a persistent metadata index.
- Unicode/punctuation normalization, season/episode parsing, a small explicit alias map, and ranking by title, year, season, episode, resolution, and language. Add aliases in `autofilter/search.py`.
- Deduplication by Telegram document identity, paginated result buttons, counts, and clear-search actions.
- Expiring bounded search cache, bounded callback sessions, global concurrency limits, per-user and per-chat rolling limits.
- Asynchronous document/video/audio copies, cached Bot API file IDs captured from channel updates, missing-file checks, protection checks, and duplicate-send throttling.
- Private root-owner panel with dashboard, source/storage/group/sudo lists and removal confirmations, search toggles, welcome settings, cache controls, connection health, and recent error types.
- PostgreSQL settings, sudo assignments, approved groups, channel roles, file metadata, user activity, counters, and administrative audit events.
- Strict numeric-ID authorization, safe welcome substitution, JSON logs that exclude request URLs and exception strings, graceful shutdown, and explicit checksum-verified migrations.

## Quick start

1. Install Python 3.12 and create a virtual environment:

   ```sh
   python3.12 -m venv .venv
   . .venv/bin/activate
   pip install --require-hashes -r requirements.lock
   cp .env.example .env
   ```

2. Create a dedicated PostgreSQL database and role. Use an administrator account only for provisioning. Example SQL (replace the placeholder password through your normal secret management):

   ```sql
   CREATE ROLE filter_user LOGIN PASSWORD 'replace_with_a_strong_password';
   CREATE DATABASE filter_bot OWNER filter_user;
   ```

   The migration role needs permission to create tables and indexes in its dedicated schema. Use a separate runtime role with SELECT/INSERT/UPDATE/DELETE and sequence access if your organization requires role separation. No PostgreSQL extensions are needed. For remote databases, use a provider-issued TLS URL with certificate verification, such as `sslmode=verify-full`; do not disable TLS verification.

3. Edit `.env` privately:

   | Setting | Purpose |
   | --- | --- |
   | `BOT_TOKEN` | BotFather bot token |
   | `API_ID`, `API_HASH` | Your application credentials from my.telegram.org |
   | `OWNER_IDS` | Comma/space separated numeric root owner IDs |
   | `DATABASE_URL` | Dedicated PostgreSQL connection URL |
   | `TELETHON_SESSION_PATH` | Path ending in `.session`, e.g. `secrets/userbot.session` |

   Existing shell environment values take precedence over `.env`. A Pyrogram string session is incompatible with this Telethon session. Owners are read from the environment on startup; sudo users cannot change them.

4. Generate the session interactively on a trusted machine:

   ```sh
   python gen_session.py
   ```

   Enter the phone number, Telegram code, and optional two-step password at the hidden prompts. The utility stores a private SQLite session file and never prints a session string. It refuses to overwrite an existing session. Treat the file as account access: never commit, send, or log it. Set permissions to `0600`, use a private parent directory, and securely mount it at the configured path on the host. Telethon may update the session, so its mount must be writable by the worker. Session generation is the only interactive login step; the worker refuses an unauthenticated session.

5. Apply migrations, check the database, and run:

   ```sh
   python bot.py --migrate
   python bot.py --check-db
   python bot.py
   ```

   `--migrate` uses a transaction and advisory lock. Already applied migrations are skipped, checksums are verified, and changed applied migrations fail. Add new numbered SQL files for upgrades; never edit an applied migration. These commands load the complete deployment configuration, but database commands do not connect to Telegram. No automatic resets or down migrations are provided. Back up PostgreSQL before upgrades; destructive migrations require separate operator approval.

## Telegram access setup

1. Create a bot through BotFather. For automatic plain-text group searches, turn off BotFather group privacy mode or make the bot a group administrator. `/search <title>` can be used explicitly.
2. Manually add the bot and the Telethon user account to each storage/source channel. The bot generally needs channel administrator access to receive channel posts and copy channel media. The user account needs channel history access. The worker never joins channels automatically.
3. Only register channels whose content you are authorized to distribute. Protected-content channels and messages are rejected. Register each searchable location in both roles:

   ```text
   /addsource -1001234567890
   /setstore -1001234567890
   ```

   Each command verifies both accounts' access. `/setstore` adds a storage location; it does not replace existing ones. In this implementation each searchable channel is also its storage location. Arbitrary source-to-storage mirrors are not inferred. Multiple locations are supported by registering each in both roles.
4. Approve a supergroup with `/approve` in that group, or `/approve <group_id>` from an owner chat. Private search is disabled by default except for root owners; toggle it through `/menu` or set `PRIVATE_SEARCH=true` before first configuration.
5. Try `Money Heist S02`, `Interstellar 1080p`, or `/search Breaking Bad S01 Hindi`.

Result buttons belong to their requesting user, chat, and message. They expire after ten minutes and on restart. Group approval, search enablement, and channel authorization are checked again for delivery. Copies do not show the original channel attribution. Captions and filenames are retained; review those for sensitive information before authorizing distribution. The service does not download content through the user account to bypass Bot API delivery restrictions.

## Commands and permissions

| Commands | Who can use them |
| --- | --- |
| `/start`, `/help`, `/search <query>` | Users in approved groups; private users when enabled; root owners in private |
| `/menu` | Root owners, private chat only |
| `/addsudo <id>`, `/delsudo <id>` | Root owners; reply-based user selection also supported |
| `/sudo` | Root owners; first page, with full pagination in the menu |
| `/approve [group_id]`, `/unapprove [group_id]` | Root owners and persisted sudo users |
| `/addsource`, `/delsource`, `/setstore`, `/delstore` with channel ID | Root owners |
| `/setwelcome <text>`, `/previewwelcome`, `/setphoto`, `/delphoto`, `/resetwelcome` | Root owners and sudo users |
| `/configure <key> <integer>` | Root owners |

Welcome configuration is global across approved groups. `/setwelcome` also accepts a reply to text; `/setphoto` accepts a reply to a Telegram photo, rather than an arbitrary URL. Welcome templates support `{name}`, `{first_name}`, `{username}`, `{id}`, and `{mention}`. User values are escaped for Telegram HTML. Supported tags include bold, italic, underline, strike, code, pre, blockquote, spoiler, and links. Unsupported tags/attributes, malformed nesting, unsafe links, and field traversal are rejected. An invalid/stale welcome photo falls back to a text greeting. New-member greetings are limited to five human members per update.

Removal commands and welcome resets require a short-lived confirmation button. Every confirmation rechecks permissions. Sudo users cannot delegate privileges, modify root owners, or use root-only callbacks. Sensitive changes produce audit rows without credential values.

## Configuration and operating limits

See `.env.example` for all deployment settings and defaults. Important limits:

- `SEARCH_LIMIT=100`: result cap; runtime configuration can reduce it, never exceed the deployment cap.
- `SOURCE_SCAN_LIMIT=100`: messages inspected per source/search variant; `MAX_SOURCES=20` bounds configured channels.
- `PAGE_SIZE=8`, `CACHE_TTL=120`, `CACHE_SIZE=200`.
- `SEARCH_CONCURRENCY=4`, `SEARCH_TIMEOUT=30` seconds; delivery uses a separate gate with the same capacity.
- `USER_RATE=10`, `CHAT_RATE=30` per `RATE_WINDOW=60` seconds. Callback interactions also consume these limits.
- `MAX_FLOOD_WAIT=15`: Telethon may sleep for short FloodWait responses; longer waits set a source-search cooldown. A total search deadline still applies.
- `MAX_QUERY_LENGTH=120`, `DB_POOL_SIZE=10` (minimum two connections).
- `DEFAULT_WELCOME` and optional `DEFAULT_PHOTO` provide initial welcome defaults.

`/configure` changes persistent `search_limit`, `cache_ttl`, `user_rate`, and `chat_rate`. Persistent settings override environment defaults. Other settings require a worker restart. The dashboard toggles search/private search and clears cached searches and result buttons after confirmation.

Run **one polling worker per database and session**. An advisory lock enforces this. Limits and cache state live in memory; multiple independent deployments sharing a Telegram token are unsupported. No bot-native `@bot inline` search is enabled because it lacks the approved group context; result lists use inline keyboards inside approved chats.

## Deployment

`Procfile` runs `worker: python bot.py`. The Docker image uses Python 3.12, a non-root user, hashed dependencies, and copies only the filter worker. Build with `docker build -t telegram-filter .`. Supply environment variables through your host's secret configuration and mount the session privately for the non-root user. Do not bake `.env` or session files into images. Run `python bot.py --migrate` as an explicit release step, then start the worker. No deployment was performed by this task.

`render.yaml` describes a worker; configure a paid/persistent disk or other writable private session mount as supported by your plan. `heroku.yml` provides a container worker. A session cannot be recovered from an ephemeral filesystem after a redeploy; arrange a persistent mount or secure secret-file restoration. Platform files are templates and require your credentials, database, and session provisioning.

This is a polling worker and does not need an HTTP server. Prefer a worker hosting plan. If your host requires HTTP readiness, add a small ASGI service that shares runtime state and returns readiness only after database and Telegram initialization; probe the running process rather than merely running a separate database check. No synthetic web page or always-healthy endpoint is included.

Use your supervisor/platform for restart-on-failure and SIGTERM shutdown. The worker disconnects Telethon and closes the PostgreSQL pool. JSON logs contain event and error types, excluding raw request URLs, credential-bearing exceptions, and traceback locals. `/menu` shows uptime, database connection health, user-session connectivity, counts, and recent error types for this process. For fuller investigation, correlate failure type and timing with provider dashboards; never enable HTTP request logging with a real bot token. `--check-db` is a database connectivity check, not a complete worker readiness probe.

## Tests and development

```sh
pip install --require-hashes -r requirements-dev.lock
ruff check .
python -m compileall -q autofilter bot.py gen_session.py
pytest -q
```

Unit tests need no Telegram credentials. The PostgreSQL test skips unless `TEST_DATABASE_URL` points to an **explicit disposable database**; it creates `af_` tables and synthetic records. It never resets or drops existing data, but must not run against production.

```sh
TEST_DATABASE_URL=postgresql://postgres:password@localhost:5432/filter_test pytest -q
```

GitHub Actions runs unit tests, real PostgreSQL repository/migration checks, lint, and compilation. `requirements.txt` pins direct runtime dependencies; `requirements.lock` and `requirements-dev.lock` pin all transitive dependencies with hashes. Regenerate intentionally with `uv pip compile --generate-hashes` for the corresponding requirements file. Ruff intentionally targets the filter worker and its tests; legacy music code has a different dependency and quality baseline.

## Architecture

```text
bot.py                         worker / --migrate / --check-db
 gen_session.py                interactive private session provisioning
autofilter/config.py           environment validation
autofilter/runtime.py          lifecycle, logging, process lock
autofilter/handlers.py         commands, panel, secure callbacks, keyboards
autofilter/permissions.py      root, sudo, and approved-group rules
autofilter/search.py           parsing, ranking, pagination, cache, throttles
autofilter/source.py           Telethon channel reads and metadata indexing
autofilter/delivery.py         Bot API delivery and duplicate protection
autofilter/welcome.py          HTML validation and safe substitutions
autofilter/database/           asyncpg repository and SQL migrations
tests/                         pure, mock-boundary, and real database tests
```

## Troubleshooting and verification limits

- **Configuration error:** verify required variable names and formats without printing secrets. Root IDs are numeric user IDs; channel/supergroup IDs start with `-100`.
- **Startup failed:** apply migrations first, verify PostgreSQL connectivity, make the session readable/writable only by the worker, and ensure it was generated with Telethon. A second worker is intentionally rejected.
- **Access check failed:** confirm both accounts are already members/admins of the numeric channel and that content protection is off. No automatic joining or protection bypass is performed.
- **No files:** use a shorter title or spelling variant. Telegram search and bounded scans do not guarantee exhaustive history retrieval. Year/quality/language are ranking signals, rather than strict exclusion filters.
- **Source unavailable:** a channel may be inaccessible, Telegram may be limiting requests, or the network may be down. Partial results are labeled; failed searches are not cached.
- **Delivery uncertain:** inspect the chat before retrying. Network timeouts can occur after Telegram accepts a send, so sends are never automatically repeated.
- **Old file references:** the worker invalidates stale Bot API references and falls back to a channel copy. Older files first use channel copies; reusable file IDs are learned from authorized channel-post updates. Large/protected/unsupported media remain subject to Bot API limits.
- **Restart:** settings, index, sudo, groups, metrics, and audit survive. Cache, active result buttons, transient rate limits, and recent error summaries reset.

Tests validate application behavior using mocked Telegram boundaries and real PostgreSQL. Real Telegram authentication, source search, bot permissions, media delivery, and host deployment require operator validation with authorized credentials. The available CodeRabbit emulator catalog does not provide Telegram Bot API or MTProto emulation. No live Telegram success is claimed. Indexing is on demand and from new channel posts; this version does not run an exhaustive background channel crawler. Deduplication recognizes the same Telegram document identity, not independently reuploaded byte-identical media. Operators must define backup, retention, monitoring, and recovery policies for their deployment; no automatic deletion of activity or audit data is performed.
