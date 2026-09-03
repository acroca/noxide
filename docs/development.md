# Development

All commands run from `assistant/`.

```bash
uv sync --dev                                    # install deps
uv run pytest tests/ -v                          # run tests      (make test)
uv run pytest tests/test_agent.py -k test_name   # one test
uv run ruff check src/ tests/                    # lint           (make lint)
```

360 tests, ~87% coverage, and they run in about two seconds — there is no
reason not to run them constantly. Pytest uses `asyncio_mode = "auto"`, so
async tests need no decorator, and HTTP is mocked with `respx`. No test should
ever make a real network call.

## Running the bot locally

No container needed. Paths default to the working directory, so:

```bash
cd assistant
cp -r ../vault.template/. ./vault     # gitignored
cp config.example.toml config.toml    # add your bot token and user id
uv run assistant auth                 # one-time device flow  (make auth)
uv run assistant run                  #                       (make run)
```

`./vault`, `./state`, `./config.toml` and any local Compose file are all
gitignored, so a development setup can never be committed by accident.

You need your own Telegram bot for this — don't develop against the one you
actually use. `/newbot` in [@BotFather](https://t.me/BotFather) is free and
takes a minute.

To develop against a container instead, copy the Compose file from
[deployment.md](deployment.md) and swap `image:` for `build: .` — deployment
manifests are documented rather than shipped, so there is no dev Compose file
in the repo.

## Layout

```
assistant/
  src/assistant/
    __main__.py     ← entry point; wires everything together
    agent.py        ← the tool-calling loop
    copilot.py      ← auth + chat completions (streaming)
    telegram_bot.py ← long polling, handlers, allowlist
    tools.py        ← vault file tools (path-jailed)
    vault_check.py  ← deterministic wiki consistency checks
    vault_check_pages.py ← page hygiene checks (links, sections, status stubs)
    schedule.py     ← APScheduler over system/schedule.md
    maintenance.py  ← built-in compile/lint jobs and their last-run state
    skills.py       ← skill discovery and loading
    web.py          ← quarantined research sub-agent
    fanout.py       ← concurrent read-only worker sub-agents
    extract.py      ← PDF/image content extraction
    transcribe.py   ← voice notes via ElevenLabs Scribe
    usage.py        ← token accounting
    lifecycle.py    ← signals and graceful shutdown
    config.py       ← TOML + env config
    prompts/*.md    ← capability prompts, assembled per run
    skills/*.md     ← shipped skills
  tests/            ← one file per module
vault.template/     ← seed users copy; never a real vault
docs/               ← this
```

[AGENTS.md](../AGENTS.md) at the repo root is the detailed architecture
reference — what each module does and, more usefully, why the non-obvious parts
are the way they are (why streaming is mandatory, why the shutdown ordering
matters, why the system prompt must stay byte-stable). Read it before changing
anything structural.

## Conventions worth knowing before you edit

- **Tools return strings, including errors.** The agent loop converts
  exceptions into `[tool error: ...]` strings for the model rather than
  crashing.
- **Missing files return a sentinel**, `[file not found: ...]`, instead of
  raising. Callers check the prefix.
- **Every content tool caps its output** and says so in the returned string.
  An uncapped one can displace the whole conversation.
- **The assembled system prompt must stay byte-stable** across runs, or the
  provider's prompt cache stops working. Anything that changes goes at the end
  — that is why the skills menu is appended last.
- **Deployment specifics stay out of Python.** Container paths, Compose
  settings and `make` targets belong in the Dockerfile and the docs.

Full house style is in [CONTRIBUTING.md](../CONTRIBUTING.md).

## Adding a tool

Three places, all in `agent.py` unless the tool has its own module:

1. A schema in `Agent._all_tools()` — plain OpenAI function-calling JSON.
2. A dispatch branch in `Agent._dispatch_tool()`, returning a string.
3. A prompt section in `src/assistant/prompts/`, if the model needs to be told
   when to reach for it. Sections are gated on the feature being wired, so an
   optional tool's prompt only loads when it is enabled.

## CI

`.github/workflows/ci.yml` runs ruff and pytest on every pull request and push
to `main`. `.github/workflows/publish-image.yml` builds and pushes the
multi-arch image to GHCR on pushes to `main` (`latest`) and on `v*.*.*` tags.

Action versions are pinned to commit SHAs. Keep it that way — and verify a SHA
actually belongs to the tag you think it does before pinning it.
