# Backup: commit attribution can blur under parallel same-file writes

**Status:** accepted limitation — revisit if it shows up in real history.

## The edge case

Backup commits are per interaction: when a run ends, a background task stages
exactly the paths that run's tool calls touched and commits them with the
exchange as the message. Runs in different rooms (and scheduled jobs) execute
in parallel, and `git add` stages whatever is on disk *now* — so if room B
writes file F in the window between room A's last write to F and A's
background commit (sub-second, since the commit fires right after the run),
then:

- A's commit includes B's change to F, under A's message;
- B's later commit may have nothing left to stage and is skipped, so B's
  interaction never appears in `git log`.

No data is ever lost and no committed content is ever wrong — the blur is in
the *message attribution* only. It needs two conversations writing the same
file within roughly the same second, which a single-user bot rarely does.

## Why not just lock

The clean fix — serializing runs that share files — was considered and
rejected for now:

- **Read-set locking** ("if the bot read it, it may write it") degenerates to
  a global lock: nearly every run reads `wiki/now.md` and `AGENTS.md`. That
  silently undoes parallel topic handling.
- **Incremental acquisition deadlocks.** The model decides what to touch as
  it goes; two runs acquiring files in opposite orders deadlock, and fixing
  that needs lock ordering or timeouts — real complexity for a cosmetic bug.
- **Lock hold times are minutes**, not milliseconds: research has a 300s tool
  budget, fan-out 1800s. A quick question in one room must not wait on a slow
  research run in another.
- The harmful version of this race (clobbered content) is already prevented
  by `rewrite_file`'s version token and `edit_file`'s exact-match refusal.

## If it becomes a problem

Signals: commits whose diff plainly contains another room's change, or
interactions that visibly produced no commit. Then the proportionate fix is
**write-set locking at run level**: a run declares the file on first write and
holds an in-memory per-file lock until its commit lands. Write-sets are small
(no `now.md`-style global funnel through *reads*), but the deadlock and
hold-time issues above still need answers before building it.
