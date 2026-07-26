# The vault: raw journal + compiled wiki

The vault follows a raw-journal + compiled-wiki design: an append-only journal records what happened; the wiki holds the compiled current state.

```
vault/
  AGENTS.md                    ← vault-specific schema: owner profile, language, local conventions
  attachments/                 ← incoming photos/files (saved by the bot)
  raw/
    journal/YYYY-MM-DD.md      ← append-only event log, one file per day
  wiki/
    now.md                     ← materialized dashboard (first read for "what do I need to do")
    routines.md                ← recurring activities: frequency, last done, next due
    index.md                   ← one line per wiki page
    log.md                     ← operations log (compile/lint)
    projects/<slug>.md         ← goal-oriented, time-bounded work
    areas/<slug>.md            ← ongoing life areas (health, family, hobbies…)
    people/<slug>.md           ← one page per recurring person
  system/                      ← managed by the bot (schedule.md, topics/, skills/)
```

The formats below are canonical in English, but a vault may localize any user-visible text — column headers, section titles, task markers, status labels. The vault's `AGENTS.md` declares the localized terms; always match what the existing files use, and never rename headings or markers back to the English defaults.

## Time rule

Everything the user reads or writes — journal entries, wiki content (including "last done" in routines), replies — uses the user's **local time**, said plainly (never UTC, never naming the timezone). UTC is reserved for system plumbing where the timezone actually matters: `system/schedule.md` timestamps and cron expressions.

## The two layers

1. **`raw/journal/`** — the permanent record. **Never edit a past day's file.** Completing a task, reversing a decision, or correcting a fact is an event *today*: record it in today's entry and update the wiki, never the old entry. Journal entries are prose only — no `- [ ]` checkboxes, no `TODO:` markers.
2. **`wiki/`** — current state, **mutated freely**. Rewrite, prune and delete on wiki pages without ceremony: history is preserved by the journal, not by strikethrough.

## Journal conventions

- One file per day: `raw/journal/YYYY-MM-DD.md`, starting with a `# YYYY-MM-DD` heading.
- Append entries as `- HH:MM prose` (local time).

## Wiki conventions

- No plugin syntax; every page has a one-line entry in `wiki/index.md`.
- Every project/area page starts with `**Status:** …` — one always-current paragraph. That is what catch-up questions read first.
- Tasks live only on project/area pages under `## Tasks`:
  - Open: `- [ ] description (due YYYY-MM-DD)` — the date is optional.
  - Done: `- [x] description (done YYYY-MM-DD)`.
  - Waiting on someone/something: append `(waiting: X)`.
  - When a page accumulates ~15 done tasks, move the oldest into an `## Archive` section at the bottom.
- Optional sections, added when they earn their place: `## Decisions` (date + decision + reasoning), `## History` (date + notable event), `## Open questions`.
- **People pages** are created on recurrence, not first mention. They hold: a relationship line, current threads (pointers to project/area pages — never restated status), the most recent ~10 notable interactions, and stated facts only. Never invent relationship or notes content.

### `wiki/routines.md`

One table of recurring activities: `| Routine | Frequency | Last done | Next due | Notes |`

- When the user confirms doing a routine, run a **full ingest** (see Ingest): journal the event, update **Last done**, recompute **Next due** right away (last done + frequency), and patch the routine's now-stale lines in `now.md`. Editing `routines.md` alone leaves the dashboard showing the routine as still pending.
- The nightly compile re-verifies **Next due** for every routine.
- "How long since I last…?" is answered from this file. Full history lives in the journal.

### `wiki/now.md`

Fully materialized dashboard — no queries, just text. Sections: **Today** (events, due/overdue routines, tasks due today), **Upcoming** (dated events and routine due dates), **Waiting** (blocked on someone/something external), **Last 7 days** (one bullet per day, newest first, synthesizing that day's journal).

Because it is a *copy* of state owned by other pages, it goes stale the moment one of them changes — an update to a routine, task, event or blocker is only half applied until the matching `now.md` lines are patched too. Rebuilt in full nightly; between rebuilds ingest keeps it current with `edit_file` line patches.

### `wiki/log.md`

Append-only log of the scheduled maintenance runs, one entry each: `## [YYYY-MM-DD] compile|lint | brief description`. Ingest does not log here — the journal already records every message-driven change.

## Operations

### Ingest (default when a message carries new information)

An ingest is not finished when the fact is filed — it is finished when no page still shows the old state. Every step below runs on every ingest.

1. **Journal it.** Append to today's journal.
2. **Update the page that owns the fact.** The project/area page; or `routines.md` (**Last done** + recomputed **Next due**) when a routine is confirmed. New project/area → create the page (with `**Status:**`) and add its `index.md` line. Just this one owning page — sibling pages are not updated unless the fact is literally wrong on them.
3. **Reconcile `now.md`.** `now.md` duplicates state that lives elsewhere, so step 2 usually leaves a line here stale. Whenever the change touched a routine, a task, an event or a blocker, **read `now.md`** — never decide from memory that it doesn't mention the item — and `edit_file` every line the change falsified. One change can falsify more than one line: a routine just done is typically listed **both** as due under **Today** and by its next-due date under **Upcoming**; a closed task may also sit under **Waiting**. Patch lines in place; never rebuild a section (**Last 7 days** is nightly-only).
4. **Check for a stale reminder.** If a reminder is noted on the item you touched and the change makes it moot or wrong, cancel it or reschedule it with corrected text.
5. **Confirm in the reply** which files changed.

Surgical, not partial. Keep each write small — `edit_file` line patches, never a wholesale rewrite of `now.md` or `index.md` — but small writes are not a license to skip step 3. The nightly compile is a safety net for what ingest could not know, not a reason to leave behind a line you know is wrong: the user reads `now.md` throughout the day, so a routine that still shows as pending hours after they reported doing it is the failure this step exists to prevent. A read of `now.md` that turns up nothing to fix is a correct ingest, not wasted effort.

When one destination is clearly the best fit, file there without asking — corrections are cheap. Ask (one short question) only when filing it wrong would actually matter.

### Query

- "What do I need to do?" → read `wiki/now.md` (and `wiki/routines.md` if routine freshness matters) and answer with the **Today** scope only: today's events, due routines, tasks due today. No Upcoming, no Waiting, no projects — those come only when asked.
- "Catch me up on X" → read that page's `**Status:**` paragraph first.
- Historical questions → search `raw/journal/` and the page's History/Decisions sections; cite dates and files.
- Valuable synthesis produced in chat gets filed back into the wiki, not left to evaporate.

### Compile (nightly scheduled job)

1. Read today's and yesterday's journal; verify the wiki reflects every event (apply anything ingest missed).
2. Recompute **Next due** for every routine.
3. Rebuild `wiki/now.md` in full.
4. Reconcile `wiki/index.md` against the pages on disk.
5. Append a compile entry to `wiki/log.md`. Message the user only if something needs attention.

### Lint (weekly scheduled job, or on demand)

- Projects with no journal mention in 30+ days → propose a status change.
- Tasks open 21+ days → surface them.
- Contradictions between a page's status and recent journal entries; orphan pages; index drift; schedule jobs referencing missing files.
- Reminders noted on a wiki item whose underlying fact has since changed → reword or cancel the stale job.
- Apply safe fixes, append a lint entry to `wiki/log.md`, and message findings — stay silent if everything is clean.
