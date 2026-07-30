# The vault

The vault is the whole point: a directory of plain markdown files that is the
bot's only durable memory. It is readable and useful without the bot — open it
in Obsidian, grep it, keep it in git.

> The bot's operating rules for the vault live in
> [`assistant/src/assistant/prompts/wiki.md`](../assistant/src/assistant/prompts/wiki.md),
> which is loaded into its system prompt on every run. That file is the
> authority; this page explains the same design for humans. If you change one,
> check the other.

## The model: raw journal + compiled wiki

Two layers, with different rules:

1. **`raw/journal/`** — the permanent record, append-only. A past day's file is
   never edited. Completing a task, reversing a decision or correcting a fact
   is an event *today*: it gets recorded in today's entry, and the wiki is
   updated. History is preserved here, not by strikethrough.
2. **`wiki/`** — current state, mutated freely. Pages are rewritten and pruned
   without ceremony, precisely because the journal already holds the history.

This is why asking "what's the status of X?" reads one paragraph instead of
forty chronological entries — the wiki page has already been compiled from
them.

## Layout

```
vault/
  AGENTS.md                    ← who you are, what language to use, local conventions
  inbox.md                     ← offline capture: write here while the bot is down
  attachments/                 ← incoming photos and files, saved as-is
  raw/
    journal/YYYY-MM-DD.md      ← append-only event log, one file per day
  wiki/
    now.md                     ← dashboard: today, upcoming, waiting, last 7 days
    routines.md                ← recurring activities: frequency, last done, next due
    index.md                   ← one line per wiki page
    log.md                     ← operations log (nightly compile, weekly lint)
    projects/<slug>.md         ← goal-oriented, time-bounded work
    areas/<slug>.md            ← ongoing life areas (health, family, hobbies…)
    people/<slug>.md           ← one page per recurring person
  system/                      ← managed by the bot: schedule.md, topics/, skills/, usage.md
```

### Conventions

- **Every date in a vault file is ISO** — `2026-08-12`, or `2026-08-12 18:00`
  with a time. Not `12 August`, not `12/08`, and never with a weekday name or a
  relative date (`tomorrow`, `next Sunday`) beside it. The reason is search: the
  wiki deliberately keeps the same fact on several pages, so when a date changes
  the bot has to be able to *find* every copy, and one written as `12 August`
  will not match a search for `2026-08-12`. A weekday label is also a second
  fact that can be wrong on its own. The bot says dates naturally in chat —
  that is where they are meant to read well. `now.md`'s **Upcoming** section is
  the sole exception: within-7-days items are labelled by weekday alone, since
  that file is always read whole and rebuilt nightly.
- **Journal entries** are `- HH:MM prose` in local time, under a `# YYYY-MM-DD`
  heading. Prose only — no checkboxes, no `TODO:` markers. The filename carries
  the date, so entries need no date in the text; being append-only, old entries
  keep whatever notation they were written with.
- **Every project and area page** opens with `**Status:** …`, one always-current
  paragraph. That is what catch-up questions read first.
- **Tasks** live only on project/area pages under `## Tasks`: `- [ ] thing (due
  YYYY-MM-DD)`, `- [x] thing (done YYYY-MM-DD)`, and `(waiting: X)` when blocked
  on someone else.
- **People pages** are created on recurrence, not first mention, and hold stated
  facts only — never invented relationship detail.
- **`now.md` is a copy**, deliberately. It duplicates state owned by other
  pages so that "what do I need to do?" is one read with no queries. The cost is
  that it goes stale the moment a source page changes, which is why keeping it
  patched is part of every ingest.

Everything user-visible can be localized — column headers, section titles, task
markers. Declare the localized terms in `AGENTS.md` and the bot will match your
files instead of reverting to the English defaults.

## Operations

**Ingest** — the default when a message carries new information. Journal it,
update the one page that owns the fact, reconcile the now-stale lines in
`now.md`, check whether a scheduled reminder about it has been made moot, and
say which files changed. An ingest is not finished when the fact is filed; it
is finished when no page still shows the old state.

**Query** — "what do I need to do?" reads `now.md` and answers with today's
scope only. "Catch me up on X" reads that page's `**Status:**` paragraph.
Historical questions search the journal and cite dates.

**Compile** — nightly. Re-reads recent journal entries, applies anything ingest
missed, recomputes every routine's next-due date, rebuilds `now.md` in full,
reconciles `index.md` against the pages on disk, logs to `log.md`.

**Lint** — weekly. Surfaces projects with no journal mention in 30+ days, tasks
open 21+ days, contradictions between a page's status and recent entries,
orphan pages, index drift, and scheduled jobs referencing missing files.

Compile and lint are ordinary scheduled jobs, not built-in machinery — see
[scheduling](#scheduling) below.

**Backup** — optional but recommended: with `[backup] enabled` the bot keeps a
local git history of the vault, one commit per interaction that changed it,
with the exchange in the commit message. History, undo, and disaster recovery
in one mechanism — see [deployment.md](deployment.md#backups).

**Offline capture** — when the bot is down but the vault is reachable (on the
host, or through your sync), write into `inbox.md` at the vault root:
free-form entries, one per paragraph or bullet, an ISO date/time up front when
the timing matters. At the next startup the bot processes every entry as if
you had texted it — notes journaled and filed, reminders scheduled, questions
answered — then clears the processed content and messages you what it did.
Entries added while it was ingesting are kept for the next round, a failed run
leaves the file untouched so nothing is lost, and with backup enabled the raw
file is committed to history before anything clears it. While the bot is
running, "process the inbox" in chat does the same on demand.

## Starting a vault

`vault.template/` in the repo is a seed, not a working vault: an empty
`AGENTS.md` skeleton and the topic index. Copy it to wherever you mounted
`/data/vault`:

```bash
cp -r vault.template/. ./vault
```

Then just talk to the bot — ask it to **"set up my vault"**. Its built-in
`vault-setup` skill interviews you for the few things the code cannot know
(your name, timezone, and reply language), writes `AGENTS.md` itself, lays
down the skeleton pages, and offers to schedule the housekeeping jobs. You
never have to hand-edit commented-out placeholders.

Starting from a completely empty directory works too; the template just saves
the bot a few guesses.

### `AGENTS.md`

Loaded fresh on every run and appended **after** the built-in instructions, so
it wins on conflict. Keep it to conventions — who you are, what language to
reply in, how this vault differs. Facts about people and projects belong in the
wiki, not here.

## Scheduling

Ask in plain language: *"schedule a daily morning brief at 8am"*. The bot
manages `system/schedule.md`, a markdown table, and you can list, change or
cancel jobs conversationally.

Two housekeeping jobs are worth setting once: a nightly **compile** and a
weekly **lint**.

`when` accepts:

- ISO 8601 datetimes — `2024-06-15T09:00:00+00:00`
- Cron expressions, when recurring — `0 8 * * *`
- Relative expressions, one-off only — `in 10 minutes`, `tomorrow at 9am`

The file is the source of truth and is re-read every 60 seconds, so hand edits
take effect within a minute. All five columns (`id`, `when`, `recurring`,
`prompt`, `created`) are required; a row that does not parse is skipped with a
warning in the log, and written back untouched when something else edits the
table — otherwise one bad row would be erased by the next write.

Times run on the local clock: a cron expression fires on your wall clock, and a
`when` you write by hand is read as local unless it carries an offset. The one
UTC form is what the `schedule` tool stores for one-off jobs — an absolute
instant with `+00:00` — which is unambiguous precisely because the offset is
there. Write cron weekdays as names (`SUN`); the numbering starts at Monday, so
`0` means Monday.

On restart, one-off jobs overdue by less than 12 hours fire once; older ones
are dropped.

A reminder whose purpose is already met stays quiet: scheduled runs read the
relevant wiki state first and stand down silently if the routine was already
logged or the task already closed.

## Skills

A skill is a markdown file holding a procedure for recurring work — your weekly
review, how you like receipts filed. Only the name and the `**Use when:**`
trigger line sit in the system prompt; the body is loaded on demand when the
trigger matches. That keeps the prompt small and stable, so refining a skill's
steps costs nothing.

Two ship with the image (`skill-authoring`, `vault-setup`). Yours go in
`system/skills/<slug>.md`, and a vault file **shadows** a shipped one with the
same name — that is how you customise a built-in. Slugs must be lowercase
letters, digits and hyphens; a file without a valid `**Use when:**` line is
excluded from the menu and logged.

Ask the bot to write one and it will. It also refines a skill in place when
following it reveals a missing step, which is the intended way they improve.

## Rooms

Turn the chat into a supergroup with Topics enabled and each forum topic
becomes its own room: separate conversation history and an optional per-room
prompt at `system/topics/<slug>/AGENTS.md`.

All rooms share **one** vault. The point is a single memory viewed from
different angles, not parallel note trees. Journal entries from a room are
prefixed with the slug (`- 09:30 [gaming] …`), and `system/topics/index.md` maps
topic id ↔ slug ↔ name. Ask the bot to *"create a topic called Health"* and it
makes the Telegram topic, the vault directories and the index entry in one go.

Rooms run in parallel: a long-running request in one room does not hold up a
message in another, while messages within one room stay strictly ordered.
Writes to the shared vault are guarded against collisions — a full-file
rewrite is accepted only if the file still matches what that conversation last
read, and refused otherwise so the bot re-reads and redoes the change instead
of overwriting someone else's. The same guard protects your own hand edits
made while the bot is mid-conversation.

## Attachments

Photos and files are saved under `attachments/` before the model sees anything.

- **Photos** are shown to the vision model on the turn they arrive, alongside
  your caption, and not re-sent afterwards — follow-up messages don't re-pay
  the image tokens.
- **Documents and videos** are stored and the model only ever gets the filename,
  MIME type and stored path. It reads the contents on demand via
  `extract_attachment`: digital PDFs parsed locally, scans and images
  transcribed by the vision model.

Your caption drives intent — *"file this receipt under finance"* files it,
*"what plant is this?"* just answers and leaves the file unlinked.

Attachment content is treated as untrusted data, never instructions. See
[SECURITY.md](../SECURITY.md#what-it-does-not-defend-against) for the limits of
that.

## Token usage

Every model call is recorded to `state/usage/usage-YYYY-MM.jsonl`, and a
rolling 7-day summary is rendered to `system/usage.md` — per day, feature and
model, including cached-token counts. Recording happens off the critical path,
so it costs nothing per message.
