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
      FOURGET_URL: ${FOURGET_URL:-}
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
# Optional — enables web research with the service below
# FOURGET_URL=http://fourget
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

### Web companion (PWA)

The companion runs beside Telegram in the same Python process. Chat is the
home screen and opens General by default. Its topic switcher reads
`system/topics/index.md`, using the same topic names and instructions as your
Telegram rooms. The pinned home chat and web topics share their conversation
archive, model context (five completed exchanges by default) and per-topic run lock. Other Telegram
chats remain isolated. Now is a read-only,
plain-text display of `wiki/now.md`, preserving all sections and Markdown as
written. Opening Now never calls the model. There are no dashboard cards,
project navigation, or task/reminder buttons; make changes through chat.
Saved conversations from the earlier project-based PWA appear under
"Previous web chats" while their referenced project/area page remains available.
They are not merged into other topics; a removed page can hide its old chat
from the switcher without deleting the archived text.

On desktop, Enter sends and Shift+Enter inserts a newline. On touch-first mobile
devices, Return inserts a newline and the adjacent send button submits. The
topic picker is a button opening a list, not another native input field. Bottom
safe-area padding is reduced while the composer is focused; iOS's own keyboard
accessory toolbar is outside the PWA's control.

For a native local run, set these environment variables before starting the
usual `assistant run` command:

```dotenv
PWA_ENABLED=true
AGENT_NAME=Juniper
PWA_ORIGIN=http://localhost:8080
# Optional, enables push registration:
PWA_PUSH_CONTACT=mailto:you@example.com
```

Open `http://localhost:8080` directly; there is no sign-in. The PWA relies entirely
on your network for access control. **Anyone who can reach it has full access to
the assistant and conversations.** Do not expose it to the internet or an
untrusted LAN. Remove `PWA_PASSWORD` from older Compose/.env configurations; it
is no longer used, and obsolete stored sessions are removed on startup.

For the Compose service above, add:

```yaml
    environment:
      # Keep the existing Telegram, timezone, and other environment entries.
      PWA_ENABLED: "true"
      AGENT_NAME: ${AGENT_NAME:-Noxide}
      PWA_HOST: "0.0.0.0"
      PWA_ORIGIN: ${PWA_ORIGIN}
      PWA_PUSH_CONTACT: ${PWA_PUSH_CONTACT:-}
    ports:
      - "127.0.0.1:8080:8080"
```

Merge these entries into the existing `environment` mapping, not a second
mapping. Keep the published port bound to host localhost. For phone access,
run `mise exec -- tailscale serve --bg http://127.0.0.1:8080` on the host and set
`PWA_ORIGIN` to the HTTPS URL it prints, without a trailing slash. Use Serve,
not public Funnel, and restrict permitted devices/users in your tailnet rules.
If you use a different host port, point Serve at that port instead.

A different private HTTPS proxy is also possible; preserve the browser's Host
header when forwarding. HTTPS alone does not restrict access. The app rejects
unexpected Host and mutation Origin headers and does not enable CORS, but a
direct client can set those headers: they are not an authentication boundary.
The app is served at the origin root, not under a URL prefix. HTTPS remains
required for phone PWA features and push notifications.

**Install and notifications.** Use the browser's Install action. On iPhone,
use Share → Add to Home Screen; supported iOS versions require installation
before web push can be enabled. Open Preferences in the app and enable
notifications explicitly. Push uses persisted device subscriptions and an
automatically generated VAPID key; keep `state_dir/webpush.pem` backed up.
The supported push hosts are Chrome/FCM, Firefox, Apple and Windows browser
push services. Unsupported endpoints are rejected, and redirects are disabled.
Notifications use the **channel name** (General or the topic name) as their title
and include the reply or reminder text (up
to 500 characters). Content can appear on your lock screen; control previews
in your device notification settings. Ordinary Telegram replies still notify
only through Telegram. A reply or reminder waits five seconds before any push
goes out; if a focused device is showing that conversation scrolled to the end
by then, no device is notified. Being open on another topic, unfocused, or
scrolled up reading older messages does not count, and a device that starts
showing the reply after the window still gets the push. Opening
one goes to the corresponding conversation. Delivery depends on the OS,
browser permissions and network; Telegram remains the primary reminder
delivery channel in this first version, with successful agent-originated
Telegram sends archived under their original topic key. Known topics show that
delivery in the matching web chat. An unknown-topic push opens General as a
fallback, but does not create an archived copy of the message in General.

**Disconnects and restarts.** The shell is available offline, but API responses
and vault pages are never put in the service-worker cache. Drafts stay in local
storage on that device, and are never submitted automatically. Preferences has
separate controls for clearing local drafts and disabling notifications. Messages accepted
by the server are persisted before processing, and use client-generated IDs
to deduplicate uncertain submissions. Closing the tab does not cancel a run.
Uncertain submissions retain both their client ID and message text in local
storage. Clear local drafts does not remove those records, to preserve
deduplication if a response was lost. Clear the site's browser data to remove
all local records; this also affects installation caches and device preferences.
On restart, unfinished messages are marked interrupted; they are not blindly
replayed because they may have performed writes. Copilot outages leave a saved
message with an explicit Retry action. The same-process retry preserves active
agent work; after restart it carries a partial-processing warning.

After the first successful service-worker installation, opening without a
server connection loads the cached interface and shows **[assistant name] is unavailable**
with a Try again button. Network failures and proxy/server errors do not clear
your saved drafts. There is no application sign-in screen. This is an offline
shell, not offline access to conversations or the Now page; browser storage
eviction can also remove the cached shell.

**App updates.** The browser checks for a new service worker when the app loads,
returns to the foreground, reconnects, and periodically while open. A ready
version shows **Update available** with a Reload button. Reload saves your open
draft before activating the new version; it is disabled while a mutation
request is in flight. Already accepted agent work continues on the server.
Other open tabs offer their own reload instead of being forcibly refreshed.
No reinstall is needed. The first upgrade from an older version without the
banner may require closing all app windows and reopening once.

The service worker serves a versioned shell bundle, never a mixture of cached
and freshly fetched UI files. The cache version is a hash of the packaged UI
files stamped by the server, so any release that changes them is a new
version; unfinished downloads do not replace the currently installed worker.

Telegram inputs (including combined bursts, voice transcripts and attachment
references) and final assistant replies are archived alongside web messages in
`state_dir/companion.sqlite3`, even when the PWA is disabled. The web timeline
labels each message Telegram or Web. Existing web exchanges are migrated once;
Telegram recording starts with this version, without downloading old backlog.
By default, five completed text exchanges are restored after restart. Older completed text
remains available through `get_history` and literal `search_history`; raw tool
protocol and reasoning are not persisted. The vault remains the authority for
current knowledge, not the chat archive.

**Reset context** (also Telegram `/clear`) starts a new automatic-context window
without deleting saved messages. Older text is still retrievable explicitly.
It dismisses pending conversation work and queued notes, but does not cancel
independent scheduled jobs. The web chat draws a "Context reset" divider at
the cut, so you can see which messages the model no longer has in front of it.
It does not change messages in Telegram itself, vault files, or backups.

There is no way to delete archived text from the app. If you need a
conversation gone from disk, stop the service and remove or edit
`state_dir/companion.sqlite3`; backups and pending queue files can still hold
copies. There is no automatic retention cutoff.
Stop the service before copying its SQLite database for a consistent backup.
Generated replies remain readable in the web timeline if Telegram delivery fails;
the delivery status is recorded separately, without rerunning completed writes.

**Images and voice.** The composer takes images three ways: the attach
button, drag and drop, and pasting a screenshot or copied picture straight into
the text field. Up to four per message; each is decoded on the device and
re-encoded as JPEG within 2000px (so iPhone HEIC photos and multi-megabyte
originals arrive as ordinary JPEGs), stored in the vault's `attachments/`
folder exactly like a Telegram photo, shown to the model for that turn only,
and rendered as a thumbnail in the timeline. The server accepts only JPEG,
PNG, WebP and GIF bodies up to 20 MB and checks the bytes match the declared
type. When `ELEVENLABS_API_KEY` is set, a microphone button records a voice
note in the browser (tap to start, tap to stop, up to five minutes) and puts
the transcript into the text field for you to review and send; nothing is
sent automatically. Recording needs microphone permission and, on iPhone, an
installed app on a supported iOS version. Without the key the button is
hidden. Token streaming, automatic background retries and replacing Telegram
entirely are not part of this version. Built-in maintenance schedules are
configured server-side.

### Voice messages

The Copilot API has no audio modality, so voice notes go through the
[ElevenLabs](https://elevenlabs.io) speech-to-text API (Scribe), which takes
Telegram's OGG/Opus voice notes directly and auto-detects the language:

1. Create an API key at <https://elevenlabs.io/app/settings/api-keys>
2. Set it as `ELEVENLABS_API_KEY` in `.env` and restart

The bot replies to the transcript directly, without echoing it back. Without
the key, voice messages get a setup hint instead.

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
- `state/` — the OAuth token, remembered chat id, usage JSONL, pending outage
  retries, maintenance bookkeeping, and the consumed inbox snapshot. With
  backup enabled, it also holds `vault.git` by default. Losing it can lose
  queued work and backup history or replay already-processed captures, not
  just require re-authentication.

`state/companion.sqlite3` is the shared Telegram/web conversation archive,
including completed context, delivery status and subscriptions.
It exists even with the PWA disabled. Back it up as private data. Only unfinished
tool protocol remains in memory; interrupted work is never restored as completed.

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

Then restart the bot. Restore `state/` deliberately: an older retry queue or
inbox checkpoint can replay work already processed, while omitting the state
can lose queued work. Protect both directories in your backups.

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
