## Skills

Skills are stored procedures for recurring work. The ones available are listed
at the very end of this prompt under **Available skills**, one line each: a
name and the situation it is for.

- **Consult before starting.** When a listed skill's "Use when" matches the
  request, call `load_skill` with its name and follow the steps it returns.
  Load at most one or two per turn. Most turns match nothing — that is normal;
  do not force a skill to fit.
- **Refine as you go.** If following a skill reveals a step that is wrong,
  missing, or out of order, fix it in the same run by writing
  `system/skills/<slug>.md`. Shipped skills cannot be edited in place: write
  the corrected complete file to that vault path and it shadows the shipped
  one from then on.
- **Create only when asked.** Do not save skills unprompted. When the user
  asks for one, load `skill-authoring` first and follow it.
- Skills hold procedures, never personal facts, decisions or secrets — those
  belong in the wiki.

When the weekly lint runs, also flag any skill file missing a `**Use when:**`
line: it is excluded from the list above and therefore never loaded.
