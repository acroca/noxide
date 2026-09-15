"""Durable chat archive and completed context, shared by Telegram and the PWA.

Transport messages are recorded before processing. Only completed text enters
automatic model context; unfinished tool protocol remains process-local.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from pathlib import Path

WEB_CHAT_ID = -(2**62)


def conversation_space(chat_id: int, thread_id: int | None) -> str:
    if chat_id == WEB_CHAT_ID:
        return f"topic:{thread_id}" if thread_id is not None else "general"
    return f"telegram:{chat_id}:{thread_id or 0}"


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
        """)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(messages)")}
        for name, definition in (
            ("source", "TEXT NOT NULL DEFAULT 'web'"),
            ("metadata", "TEXT NOT NULL DEFAULT '{}'"),
            ("generation", "INTEGER NOT NULL DEFAULT 0"),
            ("delivery", "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in columns:
                self.db.execute(f"ALTER TABLE messages ADD COLUMN {name} {definition}")
        self.db.execute("UPDATE messages SET status='interrupted', error=? WHERE status IN ('queued','running')",
                        ("Service restarted. Work may have partially completed. Review before retrying.",))
        self._import_existing_web_context()
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

    def generation(self, space):
        row = self.db.execute("SELECT generation FROM context_generations WHERE space=?", (space,)).fetchone()
        return row[0] if row else 0

    def insert(self, space, role, text, status, *, message_id=None, reply_to=None,
               source="web", metadata=None, delivery=""):
        message_id = message_id or secrets.token_hex(16)
        existing = self.get(message_id)
        if existing:
            if (existing["space"], existing["role"], existing["text"]) != (space, role, text):
                raise ValueError("Message ID already used for different content")
            return message_id
        self.db.execute("""INSERT INTO messages
            (id,space,role,text,status,created,reply_to,source,metadata,generation,delivery)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (message_id, space, role, text, status, time.time(), reply_to, source,
                         json.dumps(metadata or {}, ensure_ascii=False), self.generation(space), delivery))
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

    def save_context(self, space, exchange, timestamp, *, message_id=None, request_ids=(), note_ids=()):
        exchange_id = secrets.token_hex(16)
        generation = self.generation(space)
        # Publish context, reply, completion and note consumption atomically.
        with self.db:
            for record in exchange:
                cursor = self.db.execute("INSERT INTO context_records(space,exchange_id,role,content,completed_at,generation) VALUES (?,?,?,?,?,?)",
                                         (space, exchange_id, record["role"], record["content"], timestamp, generation))
                record["id"] = cursor.lastrowid
            if message_id:
                row = self.get(message_id)
                if row is None or row["space"] != space or row["generation"] != generation:
                    raise ValueError("Conversation changed before completion")
                self.db.execute("""INSERT INTO messages
                    (id,space,role,text,status,created,reply_to,source,generation,delivery)
                    VALUES (?,?,'assistant',?,'done',?,?,?,?,?)""",
                                (f"reply:{message_id}", space, exchange[-1]["content"], time.time(),
                                 message_id, row["source"], generation,
                                 "available" if row["source"] == "web" else "pending"))
                for request_id in set(request_ids) | {message_id}:
                    self.db.execute("UPDATE messages SET status='done',error='' WHERE id=? AND space=? AND generation=? AND status NOT IN ('deleted','dismissed')",
                                    (request_id, space, generation))
            self.db.executemany("DELETE FROM pending_notes WHERE id=?", [(i,) for i in note_ids])

    def load_context(self, space):
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM context_records WHERE space=? ORDER BY id", (space,))]

    def queue_note(self, space, content):
        self.db.execute("INSERT INTO pending_notes(space,content) VALUES (?,?)", (space, content))
        self.db.commit()

    def notes(self, space):
        return self.db.execute("SELECT * FROM pending_notes WHERE space=? ORDER BY id", (space,)).fetchall()

    def reset(self, space, *, delete=False):
        generation = self.generation(space) + 1
        self.db.execute("INSERT OR REPLACE INTO context_generations VALUES (?,?)", (space, generation))
        self.db.execute("DELETE FROM pending_notes WHERE space=?", (space,))
        # Durable queue references become tombstones, so a cold retry cannot
        # bring back work deliberately reset or deleted by the user.
        self.db.execute("UPDATE messages SET status='dismissed',error='' WHERE space=? AND status NOT IN ('done','deleted')", (space,))
        if delete:
            self.db.execute("DELETE FROM context_records WHERE space=?", (space,))
            self.db.execute("UPDATE messages SET text='',metadata='{}',status='deleted',error='' WHERE space=?", (space,))
        self.db.commit()

    def close(self):
        self.db.close()
