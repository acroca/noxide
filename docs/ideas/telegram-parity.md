# Features lost when Telegram was removed

**Status:** backlog — Telegram support went on 2026-09-21 (see the *History*
section of the root `AGENTS.md`). These are the things the Telegram transport
did that the web app does not, kept here to pick from. None of them should
bring a transport abstraction back; each is a web-app feature in its own right.
Items marked *done* were restored on 2026-09-22 and stay here for the record.

## Real losses

### Any file type as an attachment — done

*Done 2026-09-22:* PDF, plain text, Markdown, CSV and JSON upload through
the composer, with the original name carried on the message. Video and office
formats stay refused: the model can do nothing with them.

Telegram accepted documents and videos, stored them in `attachments/` with the
original file name and MIME type, and the model could read PDFs on demand
through `extract_attachment`. The web composer takes images only (JPEG, PNG,
WebP, GIF). A PDF now has to be placed in the vault's `attachments/` folder by
hand before the assistant can read it.

*Cheap to restore:* `POST /api/attachments` already reads a raw body in chunks
and sniffs it; widening the accepted types (PDF, plain text, maybe video) and
carrying the original name in the message metadata is most of the work. The
model-facing note for a non-image attachment should give the stored path,
original name and MIME type, never the bytes, as the Telegram path did.

### Message bursts — not yet a problem

*Decision 2026-09-22:* kept noted, not implemented. There is no way to share
into the app from another app, so bursts do not happen; revisit if a share
target is added.

*Update 2026-09-23:* a Shortcut can now post to `POST /api/capture`, including
from the share sheet, so sharing several items in a row is possible again.
Each share is its own request and its own thread; still not merged.

Several messages arriving within a second (a WhatsApp share, a few quick
lines) were combined into one run with one reply and a
`[N messages sent together]` header. In the web app each send is its own
thread and its own run, so a burst yields N replies, none seeing the whole.

*Options:* a short client-side hold before sending (merge quick successive
sends into one message body), or a server-side batch window on
`POST /api/messages` like the old `_BATCH_WINDOW_SECONDS` (1s from the last
arrival). Client-side is simpler and keeps one message = one row.

### Voice notes sent as messages

A Telegram voice note was transcribed and answered directly. The microphone
button puts the transcript in the composer for review, which is one more tap.
Deliberate for the app, but a "send immediately" mode (long-press, or a
preference) would recover the old flow.

### Automatic outage replay for messages — done

*Done 2026-09-22:* `Companion.replay_outages` drains rows marked
`unavailable` from the archive with the job queue's backoff; in-process it
resumes the failed turn, after a restart it reruns with an automatic-retry
warning. Interrupted work (a restart mid-run) still waits for a hand retry.

A message that hit a Copilot outage was queued durably, the user was told at
once, and it replayed by itself when Copilot answered again, even across a
restart. In the web app such a message is marked `unavailable` or
`interrupted` and waits for Retry. The retry queue still exists for one-off
jobs; extending it back to web messages means an in-process replay through
`Agent.resume` for hot items and a rerun with a provenance note for cold ones,
which is what `retry_message` did before its cold path was removed.

### Reachability without the private network

Telegram worked from any device and any client, with the conversation on
Telegram's servers. The app needs a device on the tailnet, and on iPhone it
must be installed to the Home Screen for push to work. This is a property of
the design, not a feature to add; noted so it is not forgotten when choosing
between the options below.

### Guaranteed delivery of reminders — partly done

*Done 2026-09-22:* every push records devices tried and accepted on the
archived row, the timeline shows a line when that fell short, and a reminder
still unseen half an hour after its push is pushed once more. Still open: an
escalation channel (email) for reminders flagged important.

A Telegram message is delivered and kept. Push is best effort: it carries a
500-character preview, can be suppressed by the seen mark, and can be missed.
The full reminder waits in the chat until the app is opened, so nothing is
lost, but nothing is guaranteed to reach you either.

*Options:* record delivery outcome on the archived row again (the old
`delivery` column, now unused) so the timeline can show "not delivered"; a
re-push after a while if the reminder is still unseen; an escalation channel
(email) for reminders flagged important.

### A household sharing one assistant

The allowlist let several Telegram users talk to the same assistant, each in
an isolated chat with its own history. The app has one conversation and no
accounts; anyone on the network is you. Per-device conversations would need
identity, which the app deliberately does not have.

### Delivery status in the timeline

The archive no longer records whether a reply reached the user. Before, a
failed Telegram send was marked on the row and shown in the timeline. The
push equivalent would be per-message push outcome, which is weaker (push
success is acceptance by the provider, not display).

## Replaced by an equivalent

- `/model` picker and the group-title suffix → the Model dropdown in Preferences.
- `/clear` → Reset context in Preferences.
- Reply-to a message → Reply on a thread, or swipe on a phone.
- 👀 reaction and typing indicator while researching → the "Searching the web…"
  and "Working…" status lines on the message.
- Startup and restart notices in chat → opt-in restart pushes per device.
- `/start` hello listing what the assistant accepts → gone; the Preferences
  text covers it.

## Suggested order

1. ~~Non-image uploads (smallest change, restores PDF ingestion).~~ Done.
2. ~~Delivery status on pushed reminders.~~ Done; email escalation open.
3. Client-side burst merging — deferred until the app can be shared into.
4. ~~Automatic replay of outage-failed web messages.~~ Done.
5. One-tap voice notes (send the transcript without review).
