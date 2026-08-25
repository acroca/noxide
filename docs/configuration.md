# Configuration

Every setting can come from a TOML file or an environment variable. A basic
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
| `copilot.default_model` | `DEFAULT_MODEL` | `sonnet` | Which alias from `copilot.models` to start with. Must be a key of that table |
| `copilot.models` | — | `{sonnet = "claude-sonnet-5"}` | Alias → model id map, offered by `/model`. File-only: defining extra models needs a mounted `config.toml` |
| — | `GITHUB_TOKEN` | unset | Fine-grained PAT with `models: read`, enabling voice transcription. Env-only, deliberately: it is a credential, not configuration |
| `web.fourget_url` | `FOURGET_URL` | unset | Base URL of a [4get](https://git.lolcat.ca/lolcat/4get) instance. Unset disables the `research` tool entirely |
| `assistant.timezone` | `TIMEZONE` | `UTC` | IANA name, e.g. `Europe/Madrid`. Drives every local time the bot writes or says, and cron interpretation |
| `assistant.vault_path` | `VAULT_PATH` | `./vault` | Vault directory. The Docker image sets this to `/data/vault` |
| `assistant.state_dir` | `STATE_DIR` | `./state` | OAuth token, chat id, usage JSONL. The image sets this to `/data/state` |
| `assistant.history_size` | `HISTORY_SIZE` | `40` | Messages kept in memory per chat/topic. Bigger means more context and more tokens per turn |
| `backup.enabled` | `BACKUP_ENABLED` | `false` | Local-only git history of the vault: one commit per interaction that changed it, plus a periodic sweep for edits arriving from outside the bot. Nothing is ever pushed. See [deployment.md](deployment.md#backups) |
| `backup.git_dir` | `BACKUP_GIT_DIR` | `<state_dir>/vault.git` | Where the backup repository lives. Must be **outside** the vault — a git dir inside a synced folder (iCloud, Dropbox) gets corrupted by the sync engine |

Paths are expanded and resolved, so `~/vault` and relative paths both work.
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
timezone = "Europe/Madrid"
history_size = 40
```

## Model selection

`/model` opens an inline picker of the aliases above. While a non-default model
is active the bot appends ` (alias)` to the **group title** — never to its own
name, because Telegram locks `setMyName` for roughly 18 hours after a few
changes. It reconciles that title at startup, so a run that died while switched
does not leave a stale suffix.

The selection is per-runtime and resets to `default_model` on restart. If a
model id returns a 4xx, the response body is logged verbatim — that is almost
always the fastest way to find the correct id for your plan.
