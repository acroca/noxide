# Contributing

Thanks for taking a look. This is a personal assistant that happens to be open
source, so before writing code it's worth knowing what the project is trying to
be — a proposal that fits takes far less of everyone's time than a good one
that doesn't.

## What this project is

- **Single user, self-hosted.** No multi-tenancy, no accounts, no web UI.
- **The vault is the memory.** Plain markdown on disk, readable and useful
  without this bot. Conversation history is deliberately ephemeral.
- **No LLM frameworks.** The agent loop is ~100 lines of plain Python and stays
  that way. New capabilities are tools, not abstractions.
- **Quarantine over trust.** Untrusted content gets an isolated sub-agent or an
  explicit label; capabilities stay jailed.

[docs/ideas/](docs/ideas/) has the current backlog and, just as usefully, a list
of things that were considered and rejected with reasons — worth a skim before
proposing a feature.

## Getting set up

```bash
cd assistant
uv sync --dev
uv run pytest
uv run ruff check src/ tests/
```

`uv run pytest tests/test_agent.py -k test_name` runs a single test. Tests use
`asyncio_mode = "auto"`, so async tests need no decorator, and HTTP is mocked
with `respx` — no test should ever make a real network call.

Running the bot for real needs a Telegram bot token, a Copilot licence and a
one-time `assistant auth` — see [docs/development.md](docs/development.md).

## Making a change

1. **Open an issue first** for anything beyond a bug fix or a docs correction.
   A feature that doesn't fit the constraints above is a wasted afternoon.
2. **Write the test first.** The suite covers ~87% of the source and is the
   reason this thing can be refactored at all. A change without a test that
   would have failed before it is unlikely to be merged.
3. **Run `pytest` and `ruff check` before pushing.** CI runs both on every PR.
4. **Keep commits conventional**: `feat:`, `fix:`, `refactor:`, `docs:`,
   `test:`, `chore:`, `perf:`, `ci:`.

## House style

- Comments explain *why*, not what. The codebase leans on this heavily —
  several non-obvious decisions (streaming being mandatory, the shutdown
  ordering, the stable prompt prefix) are only comprehensible because a comment
  says what would break otherwise. Match that.
- Tool functions return strings, including errors (`[tool error: ...]`). The
  agent loop turns exceptions into strings for the model rather than crashing.
- Missing files return a sentinel (`[file not found: ...]`) instead of raising.
- Deployment specifics — container paths, Compose settings — belong in the
  Dockerfile and `docs/deployment.md`, never in Python source. The repo ships
  no Compose files on purpose; the docs are their single source of truth.
- Prompts live in `src/assistant/prompts/*.md` and are assembled per-run. The
  assembled prompt must stay byte-stable across runs or the provider's prompt
  cache stops working, so don't put anything changing near the front.

## Reporting bugs

Include what you asked the bot, what it did, and the relevant log lines. Redact
your vault content — a paraphrase of the note is almost always enough.

Security issues go to [SECURITY.md](SECURITY.md), not the issue tracker.
