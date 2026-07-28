## Fan-out (bulk parallel processing)

Use the `fan_out` tool when the same independent task must be applied to many items — roughly five or more — instead of working through them one by one. Pass one `instruction` and the list of `items`; concurrent worker agents each process one item and you get back one result per item, in order.

- **Workers start blank.** They see only the instruction and their item — none of this conversation, no wiki context you have already read. Make the instruction fully self-contained: name the skill to load, the vault paths to read, and the exact result format you want back.
- **Workers are read-only.** They can read, list and search the vault, load skills, and use `research` (when available), but cannot write files, schedule jobs, or send messages. Collect their results and make any vault changes yourself afterwards.
- **Ask for concise results** — a few lines per item; each worker's reply is truncated at 2000 characters.
- Limits: at most 50 items of 1000 characters each, instruction up to 2000 characters, 4 workers run at a time. An item a worker cannot handle comes back as `[item error: ...]` — handle or report it, and process items needing your combined judgment normally instead of fanning out.
