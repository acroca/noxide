# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Noxide is a self-hosted personal assistant bot: **Telegram → GitHub Copilot API → markdown vault**. A long-polling Telegram bot runs an OpenAI-style tool-calling agent loop against the Copilot chat API. All durable memory is a directory of markdown files (the *vault*); conversation history is in-memory only and lost on restart — this is intentional. No LLM frameworks.

All Python code lives in `assistant/`; `vault.template/` at the repo root is the seed a user copies to start their own vault — its `AGENTS.md` is a skeleton of the bot's runtime system prompt, not instructions for Claude Code. A real vault never lives in the repo (`/vault/` and `assistant/vault/` are gitignored).

## Commands

All commands run from `assistant/`:

```bash
uv sync --dev              # install deps
uv run pytest tests/ -v    # run tests (make test)
uv run pytest tests/test_agent.py -k test_name   # run a single test
uv run ruff check src/ tests/                    # lint (make lint)
```

Pytest runs with `asyncio_mode = "auto"` — async tests need no decorator. HTTP is mocked with `respx`. Ruff: line-length 100, target py312, rules `E,F,I,UP,B,C4` (E501 ignored).

Running locally requires a one-time Copilot device-flow auth (`make auth`) and a `config.toml` (copy `config.example.toml`); `make run` starts the bot natively.

**Deployment is documented, not shipped.** There are no Compose or SearXNG files in this repo — `docs/deployment.md` holds them as fenced blocks for users to copy, and it is the single source of truth for them. Don't add a Compose file back "for convenience": the duplication is what let `stop_grace_period` go missing from both copies. Deployment details also stay out of the code — `vault_path`/`state_dir` default to `./vault` and `./state` relative to the working directory, and the container layout is declared as `VAULT_PATH`/`STATE_DIR` in the Dockerfile. Don't reintroduce `/data/...` paths, Compose settings, or `make` targets into Python source or runtime error strings.

## Docs

`README.md` is the shop window: pitch, feature tour, quick start, links out. Reference material lives in `docs/` — `deployment.md` (setup, Compose blocks, operations), `configuration.md` (every key and env var), `vault.md` (vault design and operations, for humans), `development.md` (dev workflow, layout), `ideas/` (backlog). Keep the README short; when a section grows past a few paragraphs it belongs in `docs/`.

`docs/vault.md` and `src/assistant/prompts/wiki.md` describe the same design — the prompt is the authority (the bot actually reads it), the doc is the human explanation. Change one, check the other.

## Architecture

Entry point `__main__.py:_run()` wires everything together (note the circular dependency between bot and agent, resolved by assigning `bot._agent` after construction):

- **`agent.py`** — the core loop (~100 lines in `Agent.run`): send messages+tools to Copilot, execute returned tool calls, append results, repeat until no tool calls (max 20 iterations, 60s per tool). Holds per-`(chat_id, thread_id)` `ConversationHistory` (deque, default 40 messages). The system prompt is assembled on every run: an embedded capability prompt (`src/assistant/prompts/*.md`, sections gated by which features are wired) + `vault/AGENTS.md` + an optional topic-specific `system/topics/<slug>/AGENTS.md` — vault instructions take precedence by coming later. The assembled prompt must stay byte-stable across runs (a changing prefix defeats the provider's prompt cache); the current time rides on each user message as a `[YYYY-MM-DD HH:MM UTC]` prefix, stamped once and frozen in history.
- **`copilot.py`** — auth chain (device flow → persisted OAuth token in `state_dir/oauth_token` → short-lived bearer, refreshed 2 min before expiry) and chat completions. **Streaming is mandatory**: the non-streaming endpoint drops tool calls for reasoning models. `_read_sse_message` aggregates the SSE stream back into a non-streaming-shaped response dict; `reasoning_text`/`reasoning_opaque` fields must round-trip through history for thinking models. Every request carries an `X-Initiator: user|agent` header (agent when the last message is a tool result) — Copilot counts premium requests only for user-initiated calls, so agent-loop follow-ups must not be marked `user`. Module-level singleton via `copilot.init()` / `copilot.get_client()`.
- **`tools.py`** — `VaultTools`: read/write/edit/append/list/search, all jailed to the vault root (`_safe_path` raises `PermissionError` on escape; the agent loop converts tool exceptions to error strings returned to the model, never crashes). `edit_file` replaces one exact snippet and refuses ambiguous edits (0 or >1 matches) — it exists so the wiki's `now.md` line patches cost a line instead of a full-file rewrite.
- **`web.py`** — optional web research (enabled by `[web] searxng_url` in config). A quarantined `Researcher` sub-agent (fresh context per call, only `web_search`/`fetch_page` tools, no vault/schedule/send access) answers a ≤400-char `research` question from the main agent. Search goes through a self-hosted SearXNG the user runs alongside the bot (no published ports; the Compose service and its `settings.yml` are in `docs/deployment.md`); page fetches are SSRF-guarded (non-global addresses blocked, redirects re-checked per hop, size/char caps). Raw web content never enters the main agent's context — the only vault→web channel is the logged question string.
- **`extract.py`** — `extract_attachment` tool backend: text files read directly, digital PDFs via the pypdf text layer (detected by an average-chars-per-page heuristic), scanned PDFs rendered with pypdfium2 and transcribed by the Copilot vision model, stored images described via vision. Output capped at 20k chars (20 text pages / 8 vision pages); sync parsing and rendering run in `asyncio.to_thread`.
- **`skills.py`** — `SkillLibrary`: stored procedures as markdown, discovered from two sources — shipped `src/assistant/skills/*.md` and vault `system/skills/*.md`, the vault shadowing the repo on slug collision (so the agent "edits" a shipped skill by writing a full copy to the vault). Each file's slug is its filename stem and its first `**Use when:**` line is the trigger; `menu()` renders slug+trigger only and `Agent._load_system_prompt` appends it **last**, so refining a skill body never changes the prompt (the prompt cache survives). Bodies load on demand through the `load_skill` tool, since repo skills live inside the package and `read_file` is vault-jailed. Model-supplied slugs are validated against `[a-z0-9-]+` before any path join.
- **`schedule.py`** — APScheduler (in-memory job store) with `vault/system/schedule.md` (a markdown table) as the source of truth. Re-parsed on startup and every 60s by a poll task, so hand edits work. One-off jobs overdue < 12h fire on restart; older ones are dropped. Scheduled jobs re-enter the agent via `run_job_fn` with `chat_id=0`. `_fire` registers its own task in `_inflight` so `drain()` can wait for a mid-run job at shutdown — these run outside the Telegram update queue, so nothing else covers them.
- **`lifecycle.py`** — signal traps and the graceful shutdown sequence, kept out of `telegram_bot.py` so process lifecycle is not the bot's business. First SIGTERM/SIGINT drains, a second abandons the drain. Order matters: `Updater.stop()` makes a final `getUpdates` with the advanced offset, which **acks every update already in the local queue** — dying after that point loses those messages permanently, so `graceful_shutdown` stops fetching, then drains the Telegram queue and in-flight scheduled jobs concurrently, and only then tears the app down. `DRAIN_BUDGET` (270s) sits under the `stop_grace_period` (5m) that `docs/deployment.md` requires the Compose service to set, so the process reports what it dropped instead of being SIGKILLed.
- **`telegram_bot.py`** — long polling, allowlist filtering (silently ignores everyone else), 4000-char message splitting, no commands except `/start`, `/model` and `/clear` (model switching is an inline-keyboard picker, not an argument; while a non-default model is selected the bot retitles the group to `<title> (<alias>)`, reconciling at startup and skipping retitles while Telegram flood-limits them — never the bot name, whose `setMyName` locks for ~18h after a few changes). Supports forum topics (multi-room): each `message_thread_id` maps to a topic slug via `vault/system/topics/index.md`, giving per-topic history and prompt (all topics share the one vault). Exposes lifecycle as discrete steps (`start`, `pending_updates`, `stop_polling`, `drain`, `close`) that `lifecycle.py` orchestrates; each shutdown step no-ops when `_app` is None so a failed startup cannot crash twice.
- **`transcribe.py`** — voice notes via GitHub Models (Phi-4-multimodal), since Copilot has no audio modality. Requires optional `GITHUB_TOKEN`; compresses to 16 kbps mono MP3 with ffmpeg and splits into chunks to stay under the 8000-token request cap.
- **`config.py`** — pydantic-settings; TOML file flattened into flat keys, env vars override (`TELEGRAM_BOT_TOKEN`, `ALLOWED_USER_IDS`, `DEFAULT_MODEL`, `TIMEZONE`, ...). `validate_for_run()` fails fast listing all problems.
- **`usage.py`** — token usage tracking: `record()` queues events in memory (zero critical-path I/O); a background task appends them to monthly JSONL files in `state_dir/usage/` and regenerates a rolling 7-day `system/usage.md` view in the vault. Module singleton via `usage.init()` / module-level `usage.record()` (no-op when uninitialized, so tests need no setup).

### Tool plumbing

Tools are plain OpenAI function schemas assembled in `Agent._all_tools()` from four sources: `VaultTools.tool_schemas()`, `Scheduler.tool_schemas()`, `SkillLibrary.tool_schemas()` (when wired), and callback-backed tools (`send_message`, `create_forum_topic`, `research`) added only when the corresponding callback was injected. Dispatch is by name in `Agent._dispatch_tool`. To add a tool: add its schema and its dispatch branch, keeping the return value a string (error strings like `[tool error: ...]` go back to the model).

### Conventions the code relies on

- Vault file reads return the sentinel string `"[file not found: ...]"` instead of raising; callers check `startswith("[file not found")`. `edit_file` mirrors this with `"[edit error: ...]"` for a missing or ambiguous match, leaving the file untouched so the model can retry with more context.
- Every tool that returns content caps it and says so in the returned string (`read_file` 100k chars, `search` 200 matches, `extract_attachment` 20k, research summary 6k). A new content tool should do the same — an uncapped one can displace the whole conversation.
- `system/schedule.md` rows need all five columns; interior blanks are real columns, so the parser must not collapse them (that bug silently dropped hand-written jobs). Unparseable rows inside the table are logged at WARNING rather than skipped in silence.
- `wiki/now.md` duplicates state owned by other wiki pages, so it is the thing ingest most easily leaves stale. `prompts/wiki.md` makes reading it a mandatory ingest step rather than a conditional one, and states the routine-completion recipe (the case that regressed) in full under `### wiki/routines.md` — a local recipe that omits `now.md` overrides the general ingest list in practice.
- A scheduled run whose final reply starts with `[silent]` is suppressed by `run_job` — `prompts/schedule.md` tells reminders to check wiki state first and reply that way when their purpose is already met (e.g. the routine was logged before the reminder fired).
- Photos are sent to the model as a multimodal message only on the turn they arrive; stored history keeps the text-only version to avoid re-paying image tokens. Documents and videos are saved to `attachments/` and the model only ever sees the stored path, original name, and MIME type — never the bytes.
- Markdown tables in the vault (`schedule.md`, `topics/index.md`) are parsed/rewritten line-by-line by hand — keep the exact header/separator formats defined in `schedule.py` and `agent.py`.
- Skill lookups return the sentinel `"[skill not found: <slug>]"`, mirroring the vault's `"[file not found: ...]"` convention; a skill file lacking a `**Use when:**` line is excluded from the prompt menu and logged at WARNING.

## CI / deployment

`.github/workflows/ci.yml` runs `ruff check` and `pytest` on every pull request and push to `main`. `.github/workflows/publish-image.yml` builds `assistant/Dockerfile` (multi-arch) and pushes `ghcr.io/acroca/noxide` on pushes to `main` (`latest`) and `v*.*.*` tags. Action versions are pinned to commit SHAs — keep it that way.
