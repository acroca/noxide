# Configuration

Settings come from TOML or environment variables; the reference marks env-only
credentials and TOML-only model aliases. A basic
deployment needs no file at all — the env vars in
[deployment.md](deployment.md) are enough.

## Precedence

**environment variable → `config.toml` → built-in default.**

Only keys actually present in the file are read, so a section that sets one key
leaves its siblings at their defaults.

`config.toml` is looked up in this order:

1. `--config <path>` on the command line
2. `./config.toml` (relative to the working directory)
3. `config.toml` next to the installed package

Copy [`assistant/config.example.toml`](../assistant/config.example.toml) to
start one.

## Reference

| TOML | Env var | Default | Meaning |
|---|---|---|---|
| `telegram.bot_token` | `TELEGRAM_BOT_TOKEN` | — | **Required.** From [@BotFather](https://t.me/BotFather) |
| `telegram.allowed_user_ids` | `ALLOWED_USER_IDS` | — | **Required.** Telegram user ids allowed to talk to the bot. TOML takes an array (`[123, 456]`), the env var a comma-separated list (`123,456`). Everyone else is silently ignored |
| `telegram.default_chat_id` | `DEFAULT_CHAT_ID` | unset | Home chat for proactive sends (startup notices, scheduled jobs). Persisted state, then this value, then the first incoming message establishes it; messages in other chats cannot retarget it. For a direct chat this is your own user id |
| `copilot.default_model` | `DEFAULT_MODEL` | `sonnet` | Which alias from `copilot.models` to start with. Must be a key of that table. When `default_family` resolves, this is only the fallback |
| `copilot.models` | — | `{sonnet = "claude-sonnet-5"}` | Alias → model id map: the offline fallback, and pinned custom ids. The `/model` picker is otherwise the live Copilot catalog (refreshed each time it opens) under the API display names — a config alias whose id the catalog lists is shown as the catalog entry |
| `copilot.default_family` | `DEFAULT_FAMILY` | unset | Model-id prefix, e.g. `claude-opus`. At startup the newest fetched model of that family becomes the default (so a new Opus release is picked up on the next restart). Unset keeps `default_model` |
| `copilot.vendors` | — | `["Anthropic", "OpenAI", "Azure OpenAI"]` | Vendors the dynamic picker offers. Copilot's own picker flag already filters out legacy and non-chat models. Models Copilot serves only on its Responses endpoint (the newer OpenAI ones) are offered too — the bot picks the endpoint per model from the catalog |
| — | `ELEVENLABS_API_KEY` | unset | [ElevenLabs](https://elevenlabs.io) API key, enabling voice transcription in Telegram and the web companion's microphone button. Env-only, deliberately: it is a credential, not configuration |
| `web.fourget_url` | `FOURGET_URL` | unset | Base URL of a [4get](https://git.lolcat.ca/lolcat/4get) instance. Unset disables the `research` tool entirely |
| `assistant.timezone` | `TIMEZONE` | `UTC` | IANA name, e.g. `Europe/Madrid`. Drives every local time the bot writes or says, and cron interpretation |
| `assistant.name` | `AGENT_NAME` | `Noxide` | Instance name for assistant identity, app labels, page titles and installation manifest. Notification titles use the channel name; the installed app identifies the instance. Trimmed, printable single-line text, 1–64 characters |
| `assistant.vault_path` | `VAULT_PATH` | `./vault` | Vault directory. The Docker image sets this to `/data/vault` |
| `assistant.state_dir` | `STATE_DIR` | `./state` | OAuth token, chat id, shared conversation archive (`companion.sqlite3`), optional push key (`webpush.pem`), usage JSONL, maintenance bookkeeping, durable outage retry queue (`pending_runs.jsonl`), and consumed inbox snapshot (`inbox.processed.md`). The image sets this to `/data/state` |
| `assistant.history_exchanges` | `HISTORY_EXCHANGES` | `5` | Complete exchanges sent automatically per conversation (positive integer), with up to 6k text characters per exchange plus truncation markers. Text context is restored from SQLite after restart; completed tool traces are discarded. Older completed text is available through `get_history`/`search_history`, including before a context reset. Active and outage-pending work stays intact outside this window |
| `pwa.enabled` | `PWA_ENABLED` | `false` | Enable the web companion in the same process; Telegram remains required |
| `pwa.host` | `PWA_HOST` | `127.0.0.1` | Web listener address. Use `0.0.0.0` inside a container, with a restricted published port or proxy |
| `pwa.port` | `PWA_PORT` | `8080` | Web listener port |
| `pwa.origin` | `PWA_ORIGIN` | `http://localhost:8080` | Exact browser origin, without a trailing slash or path. HTTPS required except on localhost; proxy must preserve Host |
| `pwa.push_contact` | `PWA_PUSH_CONTACT` | unset | `mailto:you@example.com`; enables optional web push. VAPID keys are generated in `state_dir/webpush.pem` |
| `backup.enabled` | `BACKUP_ENABLED` | `false` | Local-only git history of the vault: one commit per interaction that changed it, plus a periodic sweep for edits arriving from outside the bot. Nothing is ever pushed. See [deployment.md](deployment.md#backups) |
| `backup.git_dir` | `BACKUP_GIT_DIR` | `<state_dir>/vault.git` | Where the backup repository lives. Must be **outside** the vault — a git dir inside a synced folder (iCloud, Dropbox) gets corrupted by the sync engine |
| `maintenance.compile` | `MAINTENANCE_COMPILE` | `0 3 * * *` | Cron for the built-in nightly vault compile, on the local clock, weekdays by name. `""` disables it. See [vault.md](vault.md#operations) |
| `maintenance.lint` | `MAINTENANCE_LINT` | `0 4 * * SUN` | Cron for the built-in weekly vault lint. `""` disables it |

Paths are expanded and resolved, so `~/vault` and relative paths both work.

Noxide is the project name; each deployment can choose its own assistant name.
Set `[assistant] name = "Juniper"` or `AGENT_NAME=Juniper` and restart. The
environment variable takes precedence. Renaming preserves conversations, drafts,
subscriptions and app identity. The shell offers an update after a rename;
installed Home Screen labels may need to be renamed or re-added on iOS.
Telegram's bot profile name remains managed separately through BotFather.

The PWA has no application password or login. Remove `PWA_PASSWORD` from older
deployments; it is no longer used. Restrict the listener using private networking
such as Tailscale Serve and tailnet access rules. Anyone who can reach the
service can read conversations and use the assistant. Host/Origin checks are
browser protections, not authentication. HTTPS remains required outside localhost.

`assistant.history_size` / `HISTORY_SIZE` is retired: its raw-message count is
not interchangeable with complete exchanges. Existing values are ignored with
a startup warning; remove them or replace them with `history_exchanges = 5`.
The automatic window is not a retention limit: `state_dir/companion.sqlite3`
archives Telegram and web conversations even with the PWA disabled. No old
Telegram backlog is downloaded. Completed text is restored after restart and
older history is retrievable. Reset context (`/clear` in Telegram) keeps the
archive, but dismisses pending conversation work and queued notes; independent
scheduled jobs are not cancelled. Nothing in the app deletes archived text;
remove `companion.sqlite3` while the service is stopped if you need to. The
database also holds push subscriptions.
There is no automatic expiry; archive size and loaded
conversation text can grow over time. Back it up as private operational state.

Keep `state_dir` persistent and backed up alongside the vault. Losing it can
lose queued work and replay already-consumed inbox entries; restoring older
state can also replay retries that had already completed. It contains private
message and inbox content as well as credentials, not just disposable caches.

To intentionally move proactive delivery, stop the bot, delete
`state_dir/chat_id`, update `default_chat_id` if it is set, and restart.

## Startup validation

`assistant run` checks everything before starting and reports **all** problems
at once rather than failing on the first:

- `telegram.bot_token` is set
- `allowed_user_ids` is non-empty — otherwise the bot ignores literally everyone
- `timezone` is a valid IANA name
- `default_model` is a key of `copilot.models`
- `state_dir/oauth_token` exists — run `assistant auth` if not
- with `backup.enabled`: a `git` binary is on `PATH` and `backup.git_dir` is
  not inside the vault
- `maintenance.compile` and `maintenance.lint` are valid cron expressions with
  named weekdays, or empty
- with `pwa.enabled`: `pwa.origin` is an HTTPS origin without a path (HTTP is
  accepted only on localhost), and `pwa.push_contact` is empty or starts with `mailto:`

Configuration parsing also validates a printable, nonempty instance name of at
most 64 characters, a positive history exchange count, and a valid listener port.

A malformed `ALLOWED_USER_IDS` (non-numeric entry) fails the same way rather
than raising a traceback.

## Example

```toml
[telegram]
bot_token = "123456:ABC-DEF..."
allowed_user_ids = [123456789]
# default_chat_id = 123456789

[copilot]
default_model = "sonnet"

[copilot.models]
sonnet = "claude-sonnet-5"
opus = "claude-opus-4.8"

[web]
# fourget_url = "http://fourget"

[assistant]
name = "Juniper"
timezone = "Europe/Madrid"
history_exchanges = 5

[maintenance]
compile = "30 2 * * *"
lint = "0 4 * * SUN"
```

## Model selection

`/model` opens the live Copilot catalog, refreshed when opened, with configured
aliases as fallback/custom entries. While a non-default model
is active the bot appends ` (alias)` to the **group title** — never to its own
name, because Telegram locks `setMyName` for roughly 18 hours after a few
changes. It reconciles that title at startup, so a run that died while switched
does not leave a stale suffix.

The selection is per-runtime and resets to the resolved startup default on
restart (`default_family` when available, otherwise `default_model`). If a
model id returns a 4xx, the response body is logged verbatim — that is almost
always the fastest way to find the correct id for your plan.
