# Noxide — Personal Assistant Bot

A self-hosted personal assistant: **Telegram / optional PWA → GitHub Copilot → markdown vault**.

You talk to it in Telegram or its private-network web companion. It remembers what
you tell it by writing plain markdown files, sets reminders, does the web
research you'd otherwise open ten tabs for, reads the PDF you forwarded, and
helps you see what today looks like.

```
you  ›  had a great catch-up with Marco, he's moving to Lisbon in March
bot  ›  Noted — Marco is moving to Lisbon in March.

you  ›  remind me to send him the contract on Monday morning
bot  ›  Scheduled for Monday 09:00.

you  ›  what do I need to do today?
bot  ›  • Dentist at 16:30
        • Weekly review is 2 days overdue
```

## Why you might want this

- **Your notes stay yours.** Current knowledge is a folder of markdown files on
  your disk, independent of a database. Open it in Obsidian, grep it, put it in git,
  read it in twenty years. Delete the bot tomorrow and your notes are intact.
- **It runs on a Copilot licence you probably already pay for.** No per-token
  API billing, no second AI subscription. One GitHub Copilot seat covers it,
  and `/model` switches between Sonnet, Opus and anything else your plan
  exposes.
- **Self-hosted storage, restricted access.** Notes and chat archives live on
  your machine. Telegram access is allowlisted; the optional PWA requires private
  networking. Model requests go to GitHub Copilot; optional voice transcription
  goes to ElevenLabs. Web research workers cannot read your vault.
- **Memory that compiles, not accumulates.** Rather than a growing pile of
  notes, it keeps an append-only journal of what happened plus a wiki of
  current state, and reconciles the two nightly. Asking "what's the status of
  X?" reads one paragraph, not forty entries.
- **Direct, readable code.** Plain Python, no LangChain, no
  agent framework. If it does
  something you don't like, you can find it and change it.

It is deliberately **single-user** — a personal bot for you (or your household),
not a multi-tenant service. The optional PWA runs alongside Telegram, not instead of it.

## What it can do

| | |
|---|---|
| **Notes & memory** | Writes what you tell it into a raw journal + compiled wiki; searches before creating; never edits history |
| **Reminders & jobs** | One-off (`"in 10 minutes"`, `"tomorrow at 9am"`) and recurring cron jobs, in a markdown table you can hand-edit |
| **Web research** | Optional, via a self-hosted [4get](https://git.lolcat.ca/lolcat/4get); runs in an isolated sub-agent with no vault access |
| **Voice notes** | Transcribed via ElevenLabs Scribe; optional, needs an API key |
| **Photos** | Sent to the vision model and filed in the vault, driven by your caption |
| **Documents** | PDFs, scans and text files stored and read on demand — digital PDFs parsed locally, scans transcribed via vision |
| **Bursts** | Messages that arrive within a second of each other — a WhatsApp share, a few quick lines — are handled as one turn with one reply |
| **One conversation** | No rooms or channels to pick: say what happened and the bot works out which project or area it concerns |
| **Web companion** | Chat with pasted or attached images, voice notes, a raw read-only Now page, saved drafts and opt-in push; private-network access only |
| **Threads** | Every message starts a thread; a reply (Telegram reply-to, or Reply in the web app) continues it. The model sees the thread plus the day's recent threads as background |
| **Conversation archive** | Shared home Telegram/PWA history in SQLite; older text retrieved on demand |
| **Skills** | Stored procedures in markdown that the bot consults — and refines — as it works |
| **Bulk fan-out** | One instruction over up to 50 items, processed in parallel by read-only worker sub-agents |
| **Model switching** | `/model` picker, with the active model shown in the group title |
| **Usage tracking** | Rolling 7-day token report written to `system/usage.md` |
| **Vault backup** | Optional local git history: one commit per interaction that changed the vault, with the exchange as the commit message. Never pushes |
| **Offline inbox** | Bot down? Write into `inbox.md` in the vault; at the next startup every entry is processed as if you had texted it |

---

## Quick start

You need a GitHub account with a Copilot licence, a Telegram account, and
somewhere to run a container.

1. Create a bot with [@BotFather](https://t.me/BotFather) and note the token;
   get your own user id from [@userinfobot](https://t.me/userinfobot).
2. Make a directory with a `compose.yml` and `.env` —
   **[copy them from docs/deployment.md](docs/deployment.md#2-create-the-compose-setup)**.
3. Authenticate against Copilot once:
   ```bash
   docker compose run --rm assistant auth
   ```
4. Start it:
   ```bash
   docker compose up -d
   ```
5. Say `/start` to your bot in Telegram, then ask it to **"set up my vault"**.

Full walkthrough, optional features and operational notes:
**[docs/deployment.md](docs/deployment.md)**.

## How it works

Current knowledge lives in markdown in a directory you own — the *vault* —
with an append-only journal, current wiki state, and bot-managed operational files:

```
raw/journal/YYYY-MM-DD.md   ← append-only. What happened, never edited after the fact.
wiki/                        ← current state. Rewritten freely; the journal is the history.
  now.md                     ← today, upcoming, waiting — one read, no queries
  routines.md, projects/, areas/, people/
system/                      ← the bot's own files: schedule, skills, usage
```

New information is journalled, filed on the one page that owns it, and
reconciled into `now.md` in the same turn. A built-in nightly *compile*
rebuilds the dashboard and recomputes routine due dates; a built-in weekly
*lint* surfaces stale projects and contradictions. Both start from a
deterministic checker that enumerates what drifted — overdue tasks, mirror
gaps, dead links, placeholder status paragraphs — so the model fixes findings
instead of hunting for them.

The design, the conventions and how to start a vault:
**[docs/vault.md](docs/vault.md)**.

Conversation text lives separately in `state_dir/companion.sqlite3`, even with
the PWA disabled. The archive restores completed model context after restart;
it is evidence of past discussion, not a substitute for current vault knowledge.

## Commands

Most interactions are natural language; Telegram has three commands.

| Command | What it does |
|---------|--------------|
| `/start` | A short hello listing what the bot can accept |
| `/model` | Live Copilot model picker, with configured fallback/custom entries. The group title shows the active one. Resets to the resolved startup default on restart |
| `/clear` | Resets automatic context and dismisses pending conversation work, retaining searchable archived text and vault notes. The home chat shares this reset with the web chat |

## Web research

Optional and off by default. When enabled, the bot gains a `research` tool for
anything needing current information — prices, opening hours, news, facts it
isn't sure of. It reacts 👀 to the message that triggered a search.

**How it's isolated.** Research runs in a separate sub-agent with a fresh
context per call and exactly two tools: search and fetch. It has no vault, no
schedule, no messaging. The only thing that crosses from your side to the web
is a ≤400-character question, which is logged. Raw web content never enters the
main agent's context — only the sub-agent's summary does. Page fetches are
SSRF-guarded: non-public addresses are refused, every redirect hop is
re-checked, and the vetted IP is pinned for the connection so DNS can't be
rebound underneath it.

Setup: **[docs/deployment.md](docs/deployment.md#web-research)**.

---

## Documentation

| | |
|---|---|
| [deployment.md](docs/deployment.md) | Full setup, optional features, upgrades, backups, logs. The Compose files to copy live here |
| [configuration.md](docs/configuration.md) | Every config key and env var, precedence, startup validation |
| [vault.md](docs/vault.md) | The vault design, conventions, operations, scheduling, skills, rooms |
| [development.md](docs/development.md) | Dev setup, running locally, layout, adding a tool |
| [ideas/](docs/ideas/) | Feature backlog, and what was considered and rejected |
| [AGENTS.md](AGENTS.md) | Detailed architecture reference — what each module does and why |

## Stack

Python 3.12+ (container: 3.14) with [uv](https://docs.astral.sh/uv/) · `python-telegram-bot`
(async, long polling) · `httpx` · `APScheduler` · `pydantic-settings` ·
`dateparser` · `aiohttp` · SQLite · packaged JavaScript/CSS PWA. No LangChain or
other LLM frameworks.

## Design decisions & notes

The optional [web companion](docs/deployment.md#web-companion-pwa) opens straight
into Chat, the same conversation as your Telegram home chat. A separate Now
tab shows `wiki/now.md` as read-only text. It shares the vault and assistant,
keeps Telegram working, takes pasted or attached images and voice notes, and
supports opt-in push notifications. No separate frontend deployment.

Noxide is the project; `AGENT_NAME` / `[assistant] name` sets your instance's
name. It appears in the app, in model identity and as the push title.

The PWA has no app password: keep it behind Tailscale Serve or another restricted
private network. Anyone who can reach it can use the assistant and read its chats.

- **Small context, durable history.** The home Telegram chat and the web chat share a private SQLite archive organised in threads. A reply runs with its thread's earlier messages; a new message runs with only the newest few threads of the past day as background; earlier text is searchable on demand. Reset context (`/clear`) keeps the archive, clears that background and marks the cut with a divider in the web chat. Current knowledge still belongs in the vault. See [configuration](docs/configuration.md).
- **Allowlisted users.** `allowed_user_ids` lists the Telegram user ids that may talk to the bot; everyone else is silently ignored. Multiple ids are supported (a household sharing one assistant), but they all share one vault and one conversation per chat — this is not multi-tenancy.
- **Graceful restarts.** SIGTERM starts a drain: the bot stops fetching, finishes the in-flight run plus everything already queued, waits for any mid-run scheduled job, and only then exits. This is not politeness — stopping the updater acks every fetched update to Telegram, so a container killed mid-drain loses those messages for good. A second signal abandons the drain. The budget is 270s, which is why the Compose service must set `stop_grace_period: 5m`.
- **schedule.md as source of truth.** APScheduler uses an in-memory job store only. The markdown file is re-parsed on startup and every 60 seconds, so hand edits take effect within a minute. Rows that don't parse are logged, not silently dropped. On restart, one-off jobs overdue by less than 12 hours fire once; older ones are dropped.
- **Path jail.** All file tool calls resolve relative to the vault root. Any path escaping it raises a `PermissionError`, reported back to the agent as an error string.
- **Deployment is documented, not shipped.** There are no Compose files in this repo — they live in [docs/deployment.md](docs/deployment.md) for you to copy. The code has no container paths baked in either: paths default to the working directory, and the image declares its own layout via `VAULT_PATH`/`STATE_DIR`.
- **Untrusted content is quarantined or labelled.** Web content never reaches the main agent; attachment text and images do, and the prompt is explicit that they are data, never instructions. See [SECURITY.md](SECURITY.md) for the full threat model.

---

## Contributing

Issues and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).
Bear in mind the project is deliberately narrow: single user, self-hosted, no
LLM frameworks, markdown for current knowledge and SQLite for conversation history.
[docs/ideas/](docs/ideas/) records what's been considered and what was
rejected, and why.

## Security

Found a vulnerability? Please report it privately — see [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE) © Albert Callarisa
