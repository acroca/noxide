# Deployment

The full setup, start to finish. For the short version see the
[quick start](../README.md#quick-start); for every config key see
[configuration.md](configuration.md).

**You need:** a GitHub account with a Copilot licence, a Telegram account, and
somewhere to run a container.

> **The Compose files live here, not in the repo.** Copy them out of this page
> into your own deployment directory. Nothing to clone, nothing to keep in sync
> with a checkout — and your `.env`, vault and state never sit next to source
> code you might push.

---

## 1. Create a Telegram bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow the prompts
3. Copy the **bot token** (looks like `123456:ABC-DEF...`)
4. Find your own Telegram user ID: message [@userinfobot](https://t.me/userinfobot)

## 2. Create the Compose setup

```bash
mkdir noxide && cd noxide
mkdir vault state
```

`compose.yml`:

```yaml
services:
  assistant:
    image: ghcr.io/acroca/noxide:latest
    restart: unless-stopped
    environment:
      TELEGRAM_BOT_TOKEN: ${TELEGRAM_BOT_TOKEN}
      ALLOWED_USER_IDS: ${ALLOWED_USER_IDS}
      DEFAULT_MODEL: ${DEFAULT_MODEL:-sonnet}
      TIMEZONE: ${TIMEZONE:-UTC}
      ELEVENLABS_API_KEY: ${ELEVENLABS_API_KEY:-}
    volumes:
      - ./vault:/data/vault
      - ./state:/data/state
    # Required — see "Graceful restarts" below. Without it Docker's 10s
    # default kills the bot mid-drain and loses messages it already
    # acknowledged to Telegram.
    stop_grace_period: 5m
```

The image sets `VAULT_PATH=/data/vault` and `STATE_DIR=/data/state`, so those
two volumes are all a basic setup needs — no config file.

`.env`:

```dotenv
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
ALLOWED_USER_IDS=123456789
TIMEZONE=Europe/Madrid
# DEFAULT_MODEL=sonnet
# Optional — enables voice messages, see below
# ELEVENLABS_API_KEY=...
```

Multiple users go in `ALLOWED_USER_IDS` separated by commas. Then:

```bash
chmod 600 .env
docker compose pull
```

## 3. Copilot device-flow auth (one-time)

```bash
docker compose run --rm assistant auth
```

1. A code like `XXXX-XXXX` is printed to the console
2. Open <https://github.com/login/device>
3. Sign in and enter the code
4. The OAuth token is saved to `./state/oauth_token` with `0600` permissions

The token persists across restarts and upgrades. You only redo this if you
revoke it or lose the state directory.

## 4. Start

```bash
docker compose up -d
docker compose logs -f
```

`Ctrl+C` stops following the logs; the container keeps running.

## 5. First message

Open Telegram, find your bot, send `/start`. Then try:

```
What time is it?
Note that I had a great meeting with Alice today.
Remind me to call Marco in 30 minutes.
```

If the bot ignores you entirely, your user ID is not in `ALLOWED_USER_IDS` —
that is the designed behaviour for strangers, and the log line
`Ignoring update from user_id=...` confirms it.

Next: set up your vault — see [vault.md](vault.md#starting-a-vault).

---

## Optional features

### Voice messages

The Copilot API has no audio modality, so voice notes go through the
[ElevenLabs](https://elevenlabs.io) speech-to-text API (Scribe), which takes
Telegram's OGG/Opus voice notes directly and auto-detects the language:

1. Create an API key at <https://elevenlabs.io/app/settings/api-keys>
2. Set it as `ELEVENLABS_API_KEY` in `.env` and restart

The bot echoes what it heard (`🎙 …`) before answering, so mis-transcriptions
are obvious. Without the key, voice messages get a setup hint instead.

The ElevenLabs free tier includes some transcription hours per month — fine
for personal voice notes, and the bot tells you when it hits the limit.

### Web research

Needs a [4get](https://git.lolcat.ca/lolcat/4get) instance. Run one alongside
the bot, on the same Compose network and with no published ports.

**1.** Add the service to your `compose.yml`. The `research` profile keeps it
out of a plain `docker compose up`:

```yaml
  fourget:
    image: luuul/4get:latest
    # The image ships amd64 only; arm64 hosts (Apple Silicon) run it emulated.
    platform: linux/amd64
    profiles: [research]
    restart: unless-stopped
    environment:
      - FOURGET_SERVER_NAME=fourget
      - FOURGET_PROTO=http
```

No config file or secret is needed. Leave `FOURGET_BOT_PROTECTION` unset: it
gates the API behind a captcha the assistant cannot solve, and the service is
only reachable from the Compose network anyway.

**2.** Start it, then point the bot at it — `FOURGET_URL=http://fourget` in
`.env`, or `[web] fourget_url` in `config.toml` — and restart the assistant:

```bash
docker compose --profile research up -d
```

4get scrapes one upstream engine per query — DuckDuckGo on its default
settings, and the assistant retries through Brave when that errors or comes
back empty. Both scrapers impersonate a real browser's fingerprint, which is
what keeps self-hosting on a residential IP viable where those engines
captcha plainer clients (SearXNG, this setup's predecessor, lost DuckDuckGo,
Startpage and Brave to captcha walls in one afternoon).

See [the README](../README.md#web-research) for how research is isolated from
your vault, and [SECURITY.md](../SECURITY.md) for the threat model.

### Photos

Work out of the box — Copilot's default model does vision, no extra token.

---

## Operating

### Upgrading

```bash
docker compose pull && docker compose up -d
```

`latest` tracks `main`. For deployments you want to reason about, pin a release
tag instead: `ghcr.io/acroca/noxide:v1.0.0`.

### Backups

Everything that matters is in two directories:

- `vault/` — your notes. Plain markdown. The built-in backup below gives it a
  full git history — and an undo for anything the model gets wrong.
- `state/` — the OAuth token, the remembered chat id, and usage JSONL. Losing
  it costs you a re-auth, not data.

Conversation history is deliberately **not** persisted; it lives in memory and
is gone on restart. That is by design — see the README.

#### Vault git backup

Set `[backup] enabled = true` (or `BACKUP_ENABLED=true`) and the bot keeps a
**local-only** git history of the vault:

- Every interaction that changes the vault becomes one commit. The commit
  message carries the exchange — your message and the bot's reply (or the job
  prompt and its close, for scheduled runs) — so `git log` doubles as a record
  of what happened and how it affected the vault. (One known blur: two rooms
  writing the *same file* at nearly the same moment can land both edits in
  the first room's commit — content is never lost, only the attribution; see
  [ideas/backup-attribution-race.md](ideas/backup-attribution-race.md).)
- A sweep every few minutes (and one at startup) commits changes no run made:
  edits synced in from other devices, or writes orphaned by a crash.
- Nothing is ever pushed. There is no remote, no credentials, and private
  vault content never leaves the machine.

The repository lives **outside** the vault — by default `state/vault.git`,
configurable with `backup.git_dir`. The vault itself carries no `.git` at all,
which is what makes this safe for a vault inside iCloud Drive or Dropbox:
sync engines corrupt git internals (partial syncs, conflicted ref copies,
evicted packfiles), so the git dir must never sync. For the same reason the
sweep refuses to run while iCloud eviction placeholders (`*.icloud`) are
present, so an evicted file is never committed as a deletion — keep the vault
folder pinned ("Keep Downloaded" in Finder) if you use Optimize Mac Storage.

#### Inspecting history

A git dir alone is a complete repository; point git at it from anywhere:

```bash
alias vgit='git --git-dir="$HOME/path/to/state/vault.git"'

vgit log --stat                    # what changed, when, and why
vgit show HEAD~3:wiki/now.md       # a file as it was three commits ago
vgit diff HEAD~5 HEAD              # everything from the last five interactions
```

To also diff against the *live* vault files (`vgit status`, `vgit diff`), tell
the repo where your work tree is — this config lives outside the vault, so a
host-specific path is fine:

```bash
vgit config core.worktree "$HOME/path/to/vault"
```

To keep noisy files out of history (say, Obsidian's ever-churning workspace
state), add patterns to `vault.git/info/exclude` — same syntax as
`.gitignore`, but it lives in the git dir so the vault stays free of git
artifacts. The bot appends its own entries there and preserves yours across
restarts:

```text
.obsidian/workspace*
```

Avoid running your own `git commit` against this repo while the bot is up; a
held `index.lock` makes the bot skip that backup cycle (the next sweep picks
the changes up).

#### Restoring

To roll back a single file, write the old version back and let the bot's next
sweep commit the revert:

```bash
vgit show HEAD~2:wiki/now.md > /path/to/vault/wiki/now.md
```

For a full restore into a fresh or emptied vault directory:

```bash
git --git-dir=/path/to/state/vault.git --work-tree=/path/to/vault checkout -f main
```

Then restart the bot. `state/` needs no restore ceremony — losing it only
costs a re-auth.

### Graceful restarts

`stop_grace_period: 5m` on the service is required, not advisory.

On SIGTERM the bot drains: it stops fetching, finishes the in-flight run plus
everything already queued, waits for any mid-run scheduled job, then exits.
This is a data-integrity property. Stopping the Telegram updater performs a
final `getUpdates` that **acknowledges every update already fetched**, so those
messages will never be redelivered — killing the process between that ack and
the handler loses them permanently.

The drain budget is 270s, deliberately under the 5m grace period. If it runs
out the bot tells you how many messages it dropped so you can resend. A second
SIGTERM abandons the drain immediately, so an impatient restart is never
hostage to a wedged run.

### Logs

`docker compose logs -f`. Notable lines:

| Line | Meaning |
|---|---|
| `Ignoring update from user_id=...` | Someone not on the allowlist messaged the bot |
| `Copilot API error 4xx` | Usually a bad model id — the response body is logged |
| `Ignoring unparseable row in system/schedule.md` | A hand-edited job row is malformed and will not run |
| `Registered job <id>` | A scheduled job was picked up from `schedule.md` |
| `Dropping stale job <id>` | A one-off job was more than 12h overdue at startup |

`httpx` is pinned to WARNING on purpose: at INFO it logs full request URLs,
which for Telegram includes the bot token.
