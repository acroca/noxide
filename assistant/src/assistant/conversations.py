"""Durable chat archive and completed context, shared by Telegram and the PWA.

Transport messages are recorded before processing. Only completed text enters
automatic model context; unfinished tool protocol remains process-local.
"""

from __future__ import annotations

import bisect
import json
import re
import secrets
import sqlite3
import time
from pathlib import Path

WEB_CHAT_ID = -(2**62)


def conversation_space(chat_id: int) -> str:
    """The archive key of a conversation: the home chat (Telegram and web) is ``general``."""
    if chat_id == WEB_CHAT_ID:
        return "general"
    return f"telegram:{chat_id}"


# Spaces from before topics were removed (2026-09-17) carried a thread id:
# ``telegram:<chat>:<thread>``. Threads of one chat fold into that chat.
_THREADED_SPACE = re.compile(r"(telegram:-?\d+):\d+")
# Home-chat spaces from before topics were removed: forum-topic rooms and the
# even older per-project web chats. Both fold into the one chat.
_LEGACY_HOME_SPACE = re.compile(r"topic:\d+|wiki/.+")


def _era(eras, position):
    """The generation in force at ``position`` of a time-ordered (key, generation) list.

    Rows older than the first entry join the first era, so nothing precedes it
    with a different generation and the timeline draws no divider there.
    """
    if not eras:
        return 0
    return eras[max(position - 1, 0)][1]


class ConversationArchive:
    def __init__(self, state_dir: Path):
        path = state_dir / "companion.sqlite3"
        self.db = sqlite3.connect(path)
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY, space TEXT NOT NULL, role TEXT NOT NULL,
                text TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL,
                reply_to TEXT, error TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS message_space ON messages(space, created);
            CREATE TABLE IF NOT EXISTS context_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT, space TEXT NOT NULL,
                exchange_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
                completed_at TEXT NOT NULL, generation INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS context_space ON context_records(space, id);
            CREATE TABLE IF NOT EXISTS context_generations (
                space TEXT PRIMARY KEY, generation INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS archive_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pending_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, space TEXT NOT NULL, content TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS telegram_inputs (
                chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                request_id TEXT NOT NULL, PRIMARY KEY(chat_id,message_id)
            );
            CREATE TABLE IF NOT EXISTS telegram_outputs (
                chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                archive_id TEXT NOT NULL, PRIMARY KEY(chat_id,message_id)
            );
        """)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(messages)")}
        for name, definition in (
            ("source", "TEXT NOT NULL DEFAULT 'web'"),
            ("metadata", "TEXT NOT NULL DEFAULT '{}'"),
            ("generation", "INTEGER NOT NULL DEFAULT 0"),
            ("delivery", "TEXT NOT NULL DEFAULT ''"),
            ("thread", "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in columns:
                self.db.execute(f"ALTER TABLE messages ADD COLUMN {name} {definition}")
        if "thread" not in {row["name"] for row in self.db.execute("PRAGMA table_info(context_records)")}:
            self.db.execute("ALTER TABLE context_records ADD COLUMN thread TEXT")
        self.db.execute("UPDATE messages SET status='interrupted', error=? WHERE status IN ('queued','running')",
                        ("Service restarted. Work may have partially completed. Review before retrying.",))
        self._import_existing_web_context()
        self._flatten_threaded_spaces()
        self._merge_legacy_home_spaces()
        self._backfill_threads()
        self.db.commit()

    def _import_existing_web_context(self):
        if self.db.execute("SELECT 1 FROM archive_meta WHERE key='context_import'").fetchone():
            return
        # Old web replies already link to the submitted message. Import only
        # completed exchanges, not interrupted requests or unsolicited notes.
        for user in self.db.execute("SELECT * FROM messages WHERE role='user' AND status='done' ORDER BY created").fetchall():
            replies = self.db.execute(
                "SELECT * FROM messages WHERE reply_to=? AND role='assistant' AND status='done' ORDER BY created",
                (user["id"],),
            ).fetchall()
            if not replies:
                continue
            for row in [user, *replies]:
                self.db.execute("INSERT INTO context_records(space,exchange_id,role,content,completed_at,generation) VALUES (?,?,?,?,?,0)",
                                (row["space"], user["id"], row["role"], row["text"], str(row["created"])))
        self.db.execute("INSERT INTO archive_meta VALUES ('context_import','1')")

    def _flatten_threaded_spaces(self):
        """Fold pre-topic-removal ``telegram:<chat>:<thread>`` spaces into ``telegram:<chat>``.

        Runs once. Home-chat topics are ``_merge_legacy_home_spaces``' job.
        """
        if self.db.execute("SELECT 1 FROM archive_meta WHERE key='flat_spaces'").fetchone():
            return
        spaces = {row[0] for table in ("messages", "context_records", "context_generations", "pending_notes")
                  for row in self.db.execute(f"SELECT DISTINCT space FROM {table}")}
        for space in sorted(spaces):
            match = _THREADED_SPACE.fullmatch(space)
            if match is None:
                continue
            flat = match[1]
            for table in ("messages", "context_records", "pending_notes"):
                self.db.execute(f"UPDATE {table} SET space=? WHERE space=?", (flat, space))
            generation = max(self.generation(space), self.generation(flat))
            self.db.execute("DELETE FROM context_generations WHERE space=?", (space,))
            if generation:
                self.db.execute("INSERT OR REPLACE INTO context_generations VALUES (?,?)", (flat, generation))
        self.db.execute("INSERT INTO archive_meta VALUES ('flat_spaces','1')")

    def _merge_legacy_home_spaces(self):
        """Fold the home chat's old rooms (``topic:<id>``) and project web chats (``wiki/...``) into ``general``.

        Runs once. A merged row takes the generation of the ``general`` row
        before it in time (messages by ``created``, context records by insertion
        order), so the merged timeline draws no reset the home chat never had,
        and the automatic window — which restores only the current generation
        — picks up merged exchanges from the current era like any other. The
        seen mark becomes the newest of the merged marks, so the badge does not
        open on replies already read in their room.
        """
        if self.db.execute("SELECT 1 FROM archive_meta WHERE key='merge_home_spaces'").fetchone():
            return
        tables = ("messages", "context_records", "context_generations", "pending_notes")
        spaces = sorted({row[0] for table in tables for row in self.db.execute(f"SELECT DISTINCT space FROM {table}")
                         if _LEGACY_HOME_SPACE.fullmatch(row[0])})
        home = conversation_space(WEB_CHAT_ID)
        if spaces:
            marks = ",".join("?" * len(spaces))
            eras = self.db.execute("SELECT created, generation FROM messages WHERE space=? ORDER BY created", (home,)).fetchall()
            times = [row[0] for row in eras]
            for row in self.db.execute(f"SELECT id, created FROM messages WHERE space IN ({marks})", spaces).fetchall():
                self.db.execute("UPDATE messages SET space=?, generation=? WHERE id=?",
                                (home, _era(eras, bisect.bisect_right(times, row[1])), row[0]))
            eras = self.db.execute("SELECT id, generation FROM context_records WHERE space=? ORDER BY id", (home,)).fetchall()
            ids = [row[0] for row in eras]
            for row in self.db.execute(f"SELECT id FROM context_records WHERE space IN ({marks})", spaces).fetchall():
                self.db.execute("UPDATE context_records SET space=?, generation=? WHERE id=?",
                                (home, _era(eras, bisect.bisect_left(ids, row[0])), row[0]))
            self.db.execute(f"UPDATE pending_notes SET space=? WHERE space IN ({marks})", (home, *spaces))
            self.db.execute(f"DELETE FROM context_generations WHERE space IN ({marks})", spaces)
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='seen'").fetchone():
                through = self.db.execute(f"SELECT max(through) FROM seen WHERE space IN ({marks},?)", (*spaces, home)).fetchone()[0]
                if through is not None:
                    self.db.execute("INSERT OR REPLACE INTO seen (space, through) VALUES (?,?)", (home, through))
                self.db.execute(f"DELETE FROM seen WHERE space IN ({marks})", spaces)
        self.db.execute("INSERT INTO archive_meta VALUES ('merge_home_spaces','1')")

    def _backfill_threads(self):
        """Give rows from before threads (2026-09-18) a thread: each message its own, replies their parent's.

        Idempotent and cheap: only rows with an empty thread are touched, and
        ``insert`` sets the column, so after the first start there are none.
        Context records from before threads keep a NULL thread — they stay
        searchable through the history tools but belong to no thread's context.
        """
        self.db.execute("UPDATE messages SET thread=id WHERE thread='' AND (reply_to IS NULL OR reply_to='')")
        # Reply chains are one deep today (assistant answers user); loop anyway.
        for _ in range(8):
            changed = self.db.execute(
                "UPDATE messages SET thread=(SELECT p.thread FROM messages p WHERE p.id=messages.reply_to AND p.thread!='')"
                " WHERE thread='' AND EXISTS (SELECT 1 FROM messages p WHERE p.id=messages.reply_to AND p.thread!='')").rowcount
            if not changed:
                break
        self.db.execute("UPDATE messages SET thread=id WHERE thread=''")

    def thread_of(self, message_id):
        """The root id of the thread a message belongs to, or None for an unknown message."""
        row = self.db.execute("SELECT thread FROM messages WHERE id=?", (message_id,)).fetchone()
        return row[0] if row else None

    def generation(self, space):
        row = self.db.execute("SELECT generation FROM context_generations WHERE space=?", (space,)).fetchone()
        return row[0] if row else 0

    def insert(self, space, role, text, status, *, message_id=None, reply_to=None,
               source="web", metadata=None, delivery=""):
        """Record a message. A reply joins its parent's thread; anything else starts one."""
        message_id = message_id or secrets.token_hex(16)
        existing = self.get(message_id)
        if existing:
            if (existing["space"], existing["role"], existing["text"]) != (space, role, text):
                raise ValueError("Message ID already used for different content")
            return message_id
        thread = (self.thread_of(reply_to) if reply_to else None) or message_id
        self.db.execute("""INSERT INTO messages
            (id,space,role,text,status,created,reply_to,source,metadata,generation,delivery,thread)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (message_id, space, role, text, status, time.time(), reply_to, source,
                         json.dumps(metadata or {}, ensure_ascii=False), self.generation(space), delivery, thread))
        self.db.commit()
        return message_id

    def get(self, message_id):
        return self.db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()

    def status(self, message_id, status, error=""):
        self.db.execute("UPDATE messages SET status=?, error=? WHERE id=?", (status, error, message_id))
        self.db.commit()

    def reply(self, message_id):
        row = self.db.execute("SELECT text FROM messages WHERE id=?", (f"reply:{message_id}",)).fetchone()
        return row[0] if row else None

    def delivery(self, message_id, status, receipts=None):
        self.db.execute("UPDATE messages SET delivery=?,metadata=? WHERE id=?",
                        (status, json.dumps({"telegram_message_ids": receipts or []}), f"reply:{message_id}"))
        self.db.commit()

    def save_context(self, space, exchange, timestamp, *, thread=None, message_id=None, request_ids=()):
        exchange_id = secrets.token_hex(16)
        generation = self.generation(space)
        # Publish context, reply and completion atomically.
        with self.db:
            for record in exchange:
                cursor = self.db.execute("INSERT INTO context_records(space,exchange_id,role,content,completed_at,generation,thread) VALUES (?,?,?,?,?,?,?)",
                                         (space, exchange_id, record["role"], record["content"], timestamp, generation, thread))
                record["id"] = cursor.lastrowid
            if message_id:
                row = self.get(message_id)
                if row is None or row["space"] != space or row["generation"] != generation:
                    raise ValueError("Conversation changed before completion")
                self.db.execute("""INSERT INTO messages
                    (id,space,role,text,status,created,reply_to,source,generation,delivery,thread)
                    VALUES (?,?,'assistant',?,'done',?,?,?,?,?,?)""",
                                (f"reply:{message_id}", space, exchange[-1]["content"], time.time(),
                                 message_id, row["source"], generation,
                                 "available" if row["source"] == "web" else "pending", row["thread"]))
                for request_id in set(request_ids) | {message_id}:
                    self.db.execute("UPDATE messages SET status='done',error='' WHERE id=? AND space=? AND generation=? AND status NOT IN ('deleted','dismissed')",
                                    (request_id, space, generation))

    def load_context(self, space, thread=None):
        """Completed context records of a space, or of one thread within it, oldest first."""
        if thread is None:
            return [dict(row) for row in self.db.execute(
                "SELECT * FROM context_records WHERE space=? ORDER BY id", (space,))]
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM context_records WHERE space=? AND thread=? ORDER BY id", (space, thread))]

    def recent_threads(self, space, *, generation, since, limit, exclude=None):
        """The newest threads by last activity: root message plus the latest assistant reply.

        Background for a new message that refers to an earlier exchange
        without replying to it ("done", "pastilla tomada"). Only completed
        rows of the current generation count, so a context reset clears it.
        """
        threads = []
        rows = self.db.execute(
            "SELECT thread, max(created) AS last FROM messages WHERE space=? AND status='done'"
            " AND generation=? AND created>? GROUP BY thread ORDER BY last DESC LIMIT ?",
            (space, generation, since, limit + 1)).fetchall()
        for row in rows:
            if row["thread"] == exclude:
                continue
            root = self.db.execute("SELECT role, text, created FROM messages WHERE id=? AND status='done'", (row["thread"],)).fetchone()
            if root is None:
                continue
            reply = self.db.execute(
                "SELECT text, created FROM messages WHERE thread=? AND role='assistant' AND status='done'"
                " AND id!=? ORDER BY created DESC LIMIT 1", (row["thread"], row["thread"])).fetchone()
            threads.append({"thread": row["thread"], "root_role": root["role"], "root_text": root["text"],
                            "root_created": root["created"],
                            "reply_text": reply["text"] if reply else None})
            if len(threads) == limit:
                break
        return threads

    def record_outputs(self, chat_id, message_ids, archive_id):
        """Remember which Telegram messages carried an archived row, so replies to them find its thread."""
        self.db.executemany("INSERT OR REPLACE INTO telegram_outputs VALUES (?,?,?)",
                            [(chat_id, mid, archive_id) for mid in message_ids])
        self.db.commit()

    def resolve_telegram(self, chat_id, message_id):
        """The archived row a Telegram message corresponds to — a user input or a bot output — or None."""
        row = self.db.execute("SELECT request_id FROM telegram_inputs WHERE chat_id=? AND message_id=?",
                              (chat_id, message_id)).fetchone()
        if row is None:
            row = self.db.execute("SELECT archive_id FROM telegram_outputs WHERE chat_id=? AND message_id=?",
                                  (chat_id, message_id)).fetchone()
        return row[0] if row else None

    def reset(self, space):
        generation = self.generation(space) + 1
        self.db.execute("INSERT OR REPLACE INTO context_generations VALUES (?,?)", (space, generation))
        # Durable queue references become tombstones, so a cold retry cannot
        # bring back work deliberately reset by the user.
        self.db.execute("UPDATE messages SET status='dismissed',error='' WHERE space=? AND status NOT IN ('done','deleted')", (space,))
        self.db.commit()

    def close(self):
        self.db.close()
