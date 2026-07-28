# skill-authoring

**Use when:** the user asks to save, write, or fix a skill.

A skill is a stored procedure for work that recurs. It is not a place for
facts — people, decisions, project state and personal details belong in the
wiki. If what you are about to write is knowledge rather than a procedure,
write it to the wiki instead and say so.

## Writing a new skill

Write it to `system/skills/<slug>.md`, where the slug is lowercase words
joined by hyphens (`weekly-review`, `receipt-filing`). The slug is how the
skill is loaded, so keep it short and obvious.

The slug must be ASCII lowercase letters, digits and hyphens only — a
filename with accents, underscores or capitals is skipped and never appears
in the list. The trigger line below it should still be written in the user's
own language; only the slug needs to stay ASCII.

Shape:

```
# <slug>

**Use when:** <trigger>

## Steps
1. ...

## Gotchas
- ...
```

Only the first `**Use when:**` line found in the file is parsed; everything else is free-form.

## The trigger line is the whole game

The trigger is the only part of a skill that is always visible. Skills are
chosen by reading a list of trigger lines and nothing else — the steps are
invisible until the skill is loaded. A skill with a vague trigger is never
loaded, no matter how good its steps.

- Write the *situation*, not a summary of the contents: "Use when asked for
  the weekly review" beats "Handles the weekly review process".
- Use the words the user actually says, in the language they say them in.
  Include the phrasings you have seen them use verbatim.
- Keep it to one line, under 200 characters.
- Add a negative clause when a neighbouring skill could be confused with this
  one: "…not for one-off expense questions".
- Do not overlap with an existing trigger. Read the Available skills list
  first; if a listed skill already covers the request, refine that one instead
  of adding a near-duplicate. Overlapping triggers make every future choice
  worse.

## Refining a skill

When following a skill reveals a wrong, missing, or out-of-order step, fix it
in the same run — that is the normal way skills improve, and it needs no
permission.

- Write the full corrected file to `system/skills/<slug>.md` — `create_file`
  when no vault copy exists yet (the usual case for a shipped skill),
  `rewrite_file` when one does; keep the trigger line unless the
  situation it describes has genuinely changed. A file without a valid
  `**Use when:**` line is invisible in the list, so for a shipped skill the
  original shipped copy silently keeps being used instead.
- Record hard-won details under `## Gotchas`: the thing that went wrong, the
  edge case, the ordering that matters.
- Prune steps that turned out to be unnecessary. Shorter skills are followed
  more reliably.

A shipped skill cannot be edited in place — it lives in the read-only package,
not the vault. To change one, write the corrected **complete** file to
`system/skills/<slug>.md`; that copy shadows the shipped version from then on.

## Confirming

After writing or refining, say which file you touched in one line. Do not
paste the skill back into the chat.
