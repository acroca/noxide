"""Durable chat archive and completed context.

Messages are recorded before processing. Only completed text enters
automatic model context; unfinished tool protocol remains process-local.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from pathlib import Path

# The one conversation. Older archives carry rows of other spaces from the
# Telegram era (``telegram:<chat>``); they stay in the database, never shown.
SPACE = "general"


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
                completed_at TEXT NOT NULL, generation INTEGER NOT NULL, thread TEXT
            );
            CREATE INDEX IF NOT EXISTS context_space ON context_records(space, id);
            CREATE TABLE IF NOT EXISTS context_generations (
                space TEXT PRIMARY KEY, generation INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS archive_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            -- Telegram-era bookkeeping, dropped with Telegram support.
            DROP TABLE IF EXISTS telegram_inputs;
            DROP TABLE IF EXISTS telegram_outputs;
            DROP TABLE IF EXISTS pending_notes;
        """)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(messages)")}
        for name, definition in (
            ("metadata", "TEXT NOT NULL DEFAULT '{}'"),
            ("generation", "INTEGER NOT NULL DEFAULT 0"),
            ("thread", "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in columns:
                self.db.execute(f"ALTER TABLE messages ADD COLUMN {name} {definition}")
        if "thread" not in {row["name"] for row in self.db.execute("PRAGMA table_info(context_records)")}:
            self.db.execute("ALTER TABLE context_records ADD COLUMN thread TEXT")
        self.db.execute("UPDATE messages SET status='interrupted', error=? WHERE status IN ('queued','running')",
                        ("Service restarted. Work may have partially completed. Review before retrying.",))
        self.db.commit()

    def thread_of(self, message_id):
        """The root id of the thread a message belongs to, or None for an unknown message."""
        row = self.db.execute("SELECT thread FROM messages WHERE id=?", (message_id,)).fetchone()
        return row[0] if row else None

    def generation(self, space=SPACE):
        row = self.db.execute("SELECT generation FROM context_generations WHERE space=?", (space,)).fetchone()
        return row[0] if row else 0

    def insert(self, space, role, text, status, *, message_id=None, reply_to=None, metadata=None):
        """Record a message. A reply joins its parent's thread; anything else starts one."""
        message_id = message_id or secrets.token_hex(16)
        existing = self.get(message_id)
        if existing:
            if (existing["space"], existing["role"], existing["text"]) != (space, role, text):
                raise ValueError("Message ID already used for different content")
            return message_id
        thread = (self.thread_of(reply_to) if reply_to else None) or message_id
        self.db.execute("""INSERT INTO messages
            (id,space,role,text,status,created,reply_to,metadata,generation,thread)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (message_id, space, role, text, status, time.time(), reply_to,
                         json.dumps(metadata or {}, ensure_ascii=False), self.generation(space), thread))
        self.db.commit()
        return message_id

    def get(self, message_id):
        return self.db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()

    def merge_metadata(self, message_id, patch):
        """Merge *patch* into a row's metadata; a vanished row is silently skipped."""
        row = self.get(message_id)
        if row is None:
            return
        metadata = {**json.loads(row["metadata"] or "{}"), **patch}
        self.db.execute("UPDATE messages SET metadata=? WHERE id=?",
                        (json.dumps(metadata, ensure_ascii=False), message_id))
        self.db.commit()

    def status(self, message_id, status, error=""):
        self.db.execute("UPDATE messages SET status=?, error=? WHERE id=?", (status, error, message_id))
        self.db.commit()

    def reply(self, message_id):
        row = self.db.execute("SELECT text FROM messages WHERE id=?", (f"reply:{message_id}",)).fetchone()
        return row[0] if row else None

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
                    (id,space,role,text,status,created,reply_to,generation,thread)
                    VALUES (?,?,'assistant',?,'done',?,?,?,?)""",
                                (f"reply:{message_id}", space, exchange[-1]["content"], time.time(),
                                 message_id, generation, row["thread"]))
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

    def reset(self, space=SPACE):
        generation = self.generation(space) + 1
        self.db.execute("INSERT OR REPLACE INTO context_generations VALUES (?,?)", (space, generation))
        # Pending rows become tombstones, so a retry cannot bring back work
        # deliberately reset by the user.
        self.db.execute("UPDATE messages SET status='dismissed',error='' WHERE space=? AND status NOT IN ('done','deleted')", (space,))
        self.db.commit()

    def close(self):
        self.db.close()
