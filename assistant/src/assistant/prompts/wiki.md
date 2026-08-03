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
    archive/projects/<slug>.md ← finished or abandoned projects, out of the way
  system/                      ← managed by the bot (schedule.md, topics/, skills/)
```

The formats below are canonical in English, but a vault may localize any user-visible text — column headers, section titles, task markers, status labels. The vault's `AGENTS.md` declares the localized terms; always match what the existing files use, and never rename headings or markers back to the English defaults.

## Time and date rule

**Which clock.** Everything the user reads or writes — journal entries, wiki content (including "last done" in routines), replies — uses the user's **local time**, said plainly (never UTC, never naming the timezone). The message stamp already is local time, so no conversion is ever needed: copy the hour you were given.

`system/schedule.md` runs on the same local clock. Times you pass to the `schedule` tool are local, and a cron expression fires on the user's wall clock — so "every day at 7" is `0 7 * * *`, not an hour shifted for UTC. The tool then stores one-off jobs as an absolute UTC instant with an offset (`2026-08-12T16:00:00+00:00`); that column is plumbing, and the offset is what makes it unambiguous — never hand-write a `when` in UTC without one. Write cron weekdays as names (`SUN`, `MON`), never numbers: the numbering starts at Monday, so `0` is Monday and a job meant for Sunday fires a day early.

**Which notation.** The boundary is the vault, not the sentence: **every date written into a vault file is ISO**, and dates are spoken naturally only in replies. Prose dates (`12 August`), day/month forms (`12/08`) and weekday labels all name a day in a way `search` cannot match, so when a date changes the other copies of it cannot be found — that is how a corrected date survives on a page the correction was supposed to reach.

| Case | Vault form |
|---|---|
| Date | `YYYY-MM-DD` |
| Date and time | `YYYY-MM-DD HH:MM` — space, 24h, no comma |
| Time inside a journal file | `HH:MM` — the filename already carries the date |
| Approximate | `~YYYY-MM-DD` |
| Range | `YYYY-MM-DD → YYYY-MM-DD` |
| Either of two days | `YYYY-MM-DD or YYYY-MM-DD` |
| Clock time unknown | `YYYY-MM-DD (afternoon)` — the qualifier follows the date |
| Month or year alone | `YYYY-MM`, `YYYY` |

Two things never go into a vault file: a **weekday name beside a date** (redundant, and it can be wrong on its own), and a date stated **relative to now** (`tomorrow`, `next Sunday`, `in two weeks`) — which is true on the day it is written and false forever after. Say the weekday in replies instead, where a mistake is cheap to correct. `raw/journal/` is append-only, so past entries keep whatever notation they already have; this governs what you write from now on.

Where a weekday genuinely is required (the `now.md` header, and its **Upcoming** section), **never compute it by mental arithmetic.** Derive it by counting the exact day difference from a weekday already confirmed in the vault or in the current message timestamp — or write the date with no weekday at all. A guessed weekday next to a correct date reads as a contradiction and gets one of the two "corrected" into being wrong.

**In replies**, never speak ISO — say the date as the user would (`Thursday the 30th at 18:00`).

## The two layers

1. **`raw/journal/`** — the permanent record. **Never edit a past day's file.** Completing a task, reversing a decision, or correcting a fact is an event *today*: record it in today's entry and update the wiki, never the old entry. Journal entries are prose only — no `- [ ]` checkboxes, no `TODO:` markers.
2. **`wiki/`** — current state, **mutated freely**. Rewrite, prune and delete on wiki pages without ceremony: history is preserved by the journal, not by strikethrough.

## Journal conventions

- One file per day: `raw/journal/YYYY-MM-DD.md`, starting with a `# YYYY-MM-DD` heading.
- Append entries as `- HH:MM prose` (local time).

## Wiki conventions

- No plugin syntax; every page has a one-line entry in `wiki/index.md` (archived pages under its `## Archived` section at the bottom).
- Every project/area page starts with `**Status:** …` — one always-current paragraph. That is what catch-up questions read first.
- Tasks are owned by project/area pages (or their sub-pages), under `## Tasks` — never by the journal or people pages. `now.md` additionally mirrors every open task (see its **Tasks** section below); the owning page's copy is the authority:
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

Fully materialized dashboard — no queries, just text. Sections: **Today** (events, due/overdue routines, tasks due today — and every overdue task, kept listed with its original `(due YYYY-MM-DD)` until done, rescheduled or explicitly dropped: a task vanishing from the dashboard the day after its deadline passes is how a decision silently expires), **Upcoming** (dated events and routine due dates), **Waiting** (blocked on someone/something external), **Tasks** (every open task in the wiki — see below), **Last 7 days** (one bullet per day, newest first, synthesizing that day's journal).

**Tasks** is the complete inventory, not a selection: every open `- [ ]` on every wiki page, one line per task grouped under a link to its owning page, each keeping its `(due …)`/`(waiting: …)` markers verbatim. The dashboard is the one page the user reads to see everything on their plate, so a task filed on a page but absent here is, to the user, filed nowhere. Overlap with the sections above (an overdue task also under **Today**, a blocked one also under **Waiting**) is by design — noise is acceptable, a second place to look is not. A task enters this list the moment it is created and leaves it the moment it is closed.

**Today** may open with a **Focus** block: the user's declared current priority, with its next actions copied from the owning project pages. Only an explicit user statement creates, replaces or clears it. The nightly rebuild carries it forward — re-checking its action lines against the owning pages — and never drops or invents one: the declaration itself has no other home in the wiki, so losing the block loses it.

Because it is a *copy* of state owned by other pages, it goes stale the moment one of them changes — an update to a routine, task, event or blocker is only half applied until the matching `now.md` lines are patched too. Rebuilt in full nightly; between rebuilds ingest keeps it current with `edit_file` line patches.

**Upcoming** is the one place the vault's ISO rule relaxes, because this file is always read whole and never searched, and the nightly rebuild replaces it entirely — so nothing here has to stay findable. An item **within the next 7 days** is labelled by weekday alone (`Tuesday — …`, `Friday (evening) → Saturday (early hours) — …`), read against the `Today` header just above it as "the next Tuesday". Anything **further out keeps its ISO date**: a bare weekday three weeks away names four candidate days. The rest of the file stays ISO — the `Today` header (weekday *and* date, the anchor that makes the bare weekdays below it readable), **Waiting** deadlines, and the **Last 7 days** bullets, which are the searchable index of recent days.

### `wiki/log.md`

Append-only log of the scheduled maintenance runs, one entry each: `## [YYYY-MM-DD] compile|lint | brief description`. Ingest does not log here — the journal already records every message-driven change.

## Operations

### Ingest (default when a message carries new information)

An ingest is not finished when the fact is filed — it is finished when no page still shows the old state. Every step below runs on every ingest.

1. **Journal it.** Append to today's journal.
2. **Update the page that owns the fact.** The project/area page; or `routines.md` (**Last done** + recomputed **Next due**) when a routine is confirmed. New project/area → create the page (with `**Status:**`) and add its `index.md` line. Just this one owning page — sibling pages are not updated unless the fact is literally wrong on them.
3. **Close what the message implies.** A report of fact often completes an open task without naming it — "I created the template" closes *build the template*; "laptop shipped back" closes the return errand. When the message means something got done, became moot, or came unblocked, `search` for the item instead of trusting the page already in hand: the same task can be tracked on more than one page, and a copy left open elsewhere is exactly the "still shows the old state" this checklist exists to prevent. Close every copy, and fold duplicates into one owning page while you are there.
4. **Reconcile `now.md`.** `now.md` duplicates state that lives elsewhere, so step 2 usually leaves a line here stale. Whenever the change touched a routine, a task, an event or a blocker, **read `now.md`** — never decide from memory that it doesn't mention the item — and `edit_file` every line the change falsified. One change can falsify more than one line: a routine just done is typically listed **both** as due under **Today** and by its next-due date under **Upcoming**; a closed task may also sit under **Waiting**. A new task is also a `now.md` edit — add its line to **Tasks** (and to **Today** when due today or overdue); a closed one is removed from **Tasks** and every other section that shows it. Patch lines in place; never rebuild a section (**Last 7 days** is nightly-only).
5. **Check for a stale reminder.** If a reminder is noted on the item you touched and the change makes it moot or wrong, cancel it or reschedule it with corrected text.
6. **Confirm in the reply** which files changed.

Surgical, not partial. Keep each write small — `edit_file` line patches, never a wholesale rewrite of `now.md` or `index.md` — but small writes are not a license to skip steps 3 and 4. The nightly compile is a safety net for what ingest could not know, not a reason to leave behind a line you know is wrong: the user reads `now.md` throughout the day, so a routine that still shows as pending hours after they reported doing it is the failure this step exists to prevent. A read of `now.md` that turns up nothing to fix is a correct ingest, not wasted effort.

When one destination is clearly the best fit, file there without asking — corrections are cheap. Ask (one short question) only when filing it wrong would actually matter.

### Query

- "What do I need to do?" → read `wiki/now.md` (and `wiki/routines.md` if routine freshness matters) and answer with the **Today** scope only: today's events, due routines, tasks due today. No Upcoming, no Waiting, no projects — those come only when asked.
- "Catch me up on X" → read that page's `**Status:**` paragraph first.
- Historical questions → search `raw/journal/` and the page's History/Decisions sections; cite dates and files.
- Valuable synthesis produced in chat gets filed back into the wiki, not left to evaporate.

### Archive (when a project ends)

When the user declares a project finished or abandoned — or approves the lint's proposal to retire one — move its page out of the live wiki:

1. **Settle the page.** Write the final `**Status:**` paragraph (outcome + ISO date). Close every remaining task, or mark what was deliberately dropped; cancel or reword scheduled reminders that reference the project.
2. **Move it** to `wiki/archive/projects/<slug>.md` with `move_file`.
3. **Patch references.** `search` for the old path and update every wiki mention (people pages' thread pointers, other pages' Decisions/History). `raw/journal/` stays untouched — append-only, its path mentions are history, not links.
4. **Re-file the index line.** Move the page's `index.md` line into the `## Archived` section at the bottom (create the section on first use).
5. **Reconcile `now.md`.** A settled project has no open tasks, so usually nothing to do — but verify **Tasks** and **Waiting** no longer mention it.
6. **Journal the event** in today's entry.

If `move_file` returns an error, fix the call and retry, or report the failure and stop — **never emulate a move by copying the page**: there is no delete tool, so the copy strands the original (or a pointer file) at the old path, which is exactly the clutter archiving exists to remove.

Archived pages stay readable and searchable but are never listed as live work. Reviving a project is the same move in reverse: back to `wiki/projects/`, index line restored to the live list, journal entry.

### Compile (nightly scheduled job)

1. Read today's and yesterday's journal; verify the wiki reflects every event (apply anything ingest missed).
2. Recompute **Next due** for every routine.
3. Rebuild `wiki/now.md` in full — its **Tasks** section by sweeping every wiki page's `## Tasks`, so a task ingest failed to mirror still reaches the dashboard within a day.
4. Reconcile `wiki/index.md` against the pages on disk — pages under `wiki/archive/` belong in its `## Archived` section, everything else in the live list.
5. Append a compile entry to `wiki/log.md`. Message the user only if something needs attention — and a deadline lapsing is the canonical case: flag any task whose due date passed since the previous compile (the date of which is in `wiki/log.md`). Older overdue tasks stay visible in **Today** but are not re-announced nightly; escalating them is the lint's job.

### Lint (weekly scheduled job, or on demand)

- Projects with no journal mention in 30+ days → propose archiving (see Archive above); never archive without the user's yes.
- Open tasks or `now.md` mentions on a page under `wiki/archive/` → the page was retired too early; surface it.
- Tasks open 21+ days → surface them.
- Contradictions between a page's status and recent journal entries; orphan pages; index drift; schedule jobs referencing missing files.
- Reminders noted on a wiki item whose underlying fact has since changed → reword or cancel the stale job.
- Scheduled job prompts (`system/schedule.md` rows) that hand-write the `[scheduled run]` tag or restate the scheduled-run close contract ("reply [silent]" and variants) → rewrite the row's prompt without them.
- The same task tracked on more than one page → close or consolidate to one owning page (`now.md`'s mirror lines are the mechanism, not a duplicate — but do verify **Tasks** lists exactly the open tasks the pages hold).
- Non-ISO dates in `wiki/` — day/month forms, month names, relative dates (`tomorrow`, `next Sunday`), weekday labels outside `now.md` → rewrite to `YYYY-MM-DD`. Skip `wiki/log.md`, which is append-only like the journal. Then check every weekday label that legitimately remains against the date beside it, and every relative date for having gone stale; both are how a wrong day survives in the wiki.
- Apply safe fixes, append a lint entry to `wiki/log.md`, and message findings — stay silent if everything is clean.
