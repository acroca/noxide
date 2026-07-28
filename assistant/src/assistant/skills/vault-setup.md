# vault-setup

**Use when:** the user asks to set up, initialise or bootstrap the vault — or when `AGENTS.md` is still the unfilled template and they start telling you who they are.

A fresh vault is empty apart from `AGENTS.md`, and that file ships as a
skeleton of HTML-comment placeholders. Everything the vault schema needs is
already in your instructions; the only things missing are the ones no code can
know: who the owner is, what language to use, and any local conventions. This
skill fills those in and lays down the directory skeleton.

## 1. Check whether setup is actually needed

Read `AGENTS.md`. If its sections still contain only `<!-- ... -->` comments,
the vault is unconfigured. If the profile is already filled in, do not re-run
setup — answer the request normally, or amend the one section they asked about.

## 2. Ask once

Setup needs four facts. Ask for them in a **single** short message, not one
question per turn:

- Name (how you should address them)
- City or timezone — this drives every local-time conversion you do
- The language replies should be written in
- Anything they already know they want done differently

If they only answer some of it, write what you have and leave the rest of the
section empty. Never guess a name, a birthday, or a location.

## 3. Write `AGENTS.md`

Rewrite the file with `rewrite_file` (read it first — the version token in the
read output is required), keeping the existing `##` headings and the
explanatory paragraph at the top. Replace each comment block with real prose:

- **Language** — state the reply language plainly. Delete the whole section if
  everything is English.
- **User profile** — name, location, timezone. Only what they told you.
- **Localized terms** — fill this in **only** if the vault will not be kept in
  English. Map each localized form to the English default it replaces (table
  headers, `now.md` section titles, task markers), so the operations in your
  built-in instructions still apply unambiguously.
- **Local conventions** — delete if they had nothing to add.

## 4. Lay down the skeleton

Create the pages the daily loop reads, so nothing has to be invented later:

- `wiki/now.md` — the four headings (**Today**, **Upcoming**, **Waiting**,
  **Last 7 days**), all empty.
- `wiki/routines.md` — just the table header row and separator.
- `wiki/index.md` — a heading and one line per page you just created.
- `raw/journal/<today>.md` — today's `# YYYY-MM-DD` heading plus one entry
  recording that the vault was set up.

Use the localized headings from step 3 if the vault is not in English.
Do not create `projects/`, `areas/` or `people/` pages — those appear when
there is something real to put in them.

## 5. Offer the maintenance jobs

Ask whether to schedule the two housekeeping runs, and schedule them only if
they say yes:

- nightly **compile** (a good default is 03:00 local)
- weekly **lint**

## 6. Confirm

One short message: which files you created, and one sentence on what to do
next — just start telling them things. Do not paste the vault schema back.

## Gotchas

- The profile section is the one thing that changes how every later reply is
  worded. An empty timezone means every local-time conversion is guesswork, so
  it is worth the one question even if they gave you everything else.
- `AGENTS.md` is loaded on every run and takes precedence over your built-in
  instructions. Keep it short — conventions only, never facts about people or
  projects. Those belong in the wiki.
- If the user is mid-conversation about something else, do not derail into
  setup. File what they said, then offer to set the vault up properly.
