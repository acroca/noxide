# Noxide — Personal Assistant Bot

A self-hosted personal assistant: **Telegram → GitHub Copilot → markdown vault**.

You talk to it in a Telegram chat like you'd text a person. It remembers what
you tell it by writing plain markdown files, sets reminders, does the web
research you'd otherwise open ten tabs for, reads the PDF you forwarded, and
tells you each morning what today looks like.

```
you  ›  had a great catch-up with Marco, he's moving to Lisbon in March
bot  ›  Noted in wiki/people/marco.md and today's journal.

you  ›  remind me to send him the contract on Monday morning
bot  ›  Scheduled for Monday 09:00.

you  ›  what do I need to do today?
bot  ›  • Dentist at 16:30
        • Weekly review is 2 days overdue
```

## Why you might want this

- **Your notes stay yours.** The memory is a folder of markdown files on your
  disk — no database, no vendor. Open it in Obsidian, grep it, put it in git,
  read it in twenty years. Delete the bot tomorrow and your notes are intact.
- **It runs on a Copilot licence you probably already pay for.** No per-token
  API billing, no second AI subscription. One GitHub Copilot seat covers it,
  and `/model` switches between Sonnet, Opus and anything else your plan
  exposes.
- **Nobody else's server sees your life.** One container on your own machine,
  outbound connections only, an allowlist of exactly the Telegram accounts you
  name. Web research runs through a quarantined sub-agent that is structurally
  incapable of reading your vault.
- **Memory that compiles, not accumulates.** Rather than a growing pile of
  notes, it keeps an append-only journal of what happened plus a wiki of
  current state, and reconciles the two nightly. Asking "what's the status of
  X?" reads one paragraph, not forty entries.
- **Small enough to read.** ~1,800 lines of plain Python, no LangChain, no
  agent framework. The tool-calling loop is about a hundred lines. If it does
  something you don't like, you can find it and change it.

It is deliberately **single-user** — a personal bot for you (or your household),
not a product. There is no multi-tenancy and no web UI.

## What it can do

| | |
|---|---|
| **Notes & memory** | Writes what you tell it into a raw journal + compiled wiki; searches before creating; never edits history |
| **Reminders & jobs** | One-off (`"in 10 minutes"`, `"tomorrow at 9am"`) and recurring cron jobs, in a markdown table you can hand-edit |
| **Web research** | Optional, via a self-hosted SearXNG; runs in an isolated sub-agent with no vault access |
| **Voice notes** | Transcribed via GitHub Models; optional, needs a free PAT |
| **Photos** | Sent to the vision model and filed in the vault, driven by your caption |
| **Documents** | PDFs, scans and text files stored and read on demand — digital PDFs parsed locally, scans transcribed via vision |
| **Rooms** | Telegram forum topics become separate rooms with their own history and prompt, sharing one vault |
| **Skills** | Stored procedures in markdown that the bot consults — and refines — as it works |
| **Model switching** | `/model` picker, with the active model shown in the group title |
| **Usage tracking** | Rolling 7-day token report written to `system/usage.md` |

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

Everything the bot remembers is markdown in a directory you own — the *vault* —
split into two layers with different rules:

```
raw/journal/YYYY-MM-DD.md   ← append-only. What happened, never edited after the fact.
wiki/                        ← current state. Rewritten freely; the journal is the history.
  now.md                     ← today, upcoming, waiting — one read, no queries
  routines.md, projects/, areas/, people/
system/                      ← the bot's own files: schedule, skills, topics, usage
```

New information is journalled, filed on the one page that owns it, and
reconciled into `now.md` in the same turn. A nightly *compile* rebuilds the
dashboard and recomputes routine due dates; a weekly *lint* surfaces stale
projects and contradictions.

The design, the conventions and how to start a vault:
**[docs/vault.md](docs/vault.md)**.

## Commands

Everything is natural language; there are only three commands.

| Command | What it does |
|---------|--------------|
| `/start` | A short hello listing what the bot can accept |
| `/model` | Inline picker of your configured models. The group title shows the active one. Resets to the default on restart |
| `/clear` | Forgets the current chat or topic's conversation. Vault notes are untouched |

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

Python 3.13 with [uv](https://docs.astral.sh/uv/) · `python-telegram-bot`
(async, long polling) · `httpx` · `APScheduler` · `pydantic-settings` ·
`dateparser`. No LangChain or other LLM frameworks — the agent loop is ~100
lines of plain Python.

## Design decisions & notes

- **No persistent chat history.** Restarting loses conversation context. The vault is the only memory. This is intentional — what matters got written down, and what didn't wasn't worth keeping.
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
LLM frameworks, markdown vault as the only durable memory.
[docs/ideas/](docs/ideas/) records what's been considered and what was
rejected, and why.

## Security

Found a vulnerability? Please report it privately — see [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE) © Albert Callarisa
