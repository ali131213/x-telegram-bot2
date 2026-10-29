"""Transactional registry. SQLite compatibility or persistent PostgreSQL."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class Session:
    def __init__(self, connection, postgres):
        self.connection, self.postgres = connection, postgres

    def execute(self, sql, values=()):
        return self.connection.execute(sql.replace("?", "%s") if self.postgres else sql, values)


class Store:
    def __init__(self, path: Path, database_url: str = ""):
        self.database_url = database_url
        self.persistent = bool(database_url)
        self.db = None
        if not self.persistent:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(path)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode=WAL")
        serial = "BIGSERIAL PRIMARY KEY" if self.persistent else "INTEGER PRIMARY KEY"
        statements = [
            "CREATE TABLE IF NOT EXISTS channels(id BIGINT PRIMARY KEY, title TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS updates(id BIGINT PRIMARY KEY, received_at DOUBLE PRECISION NOT NULL)",
            "CREATE TABLE IF NOT EXISTS user_aliases(user_id BIGINT PRIMARY KEY, label TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS settings_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
            f"""CREATE TABLE IF NOT EXISTS outbox(
                id {serial}, chat_id BIGINT NOT NULL, kind TEXT NOT NULL,
                file_id TEXT, body TEXT NOT NULL, extra TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, next_at DOUBLE PRECISION NOT NULL DEFAULT 0)""",
            """CREATE TABLE IF NOT EXISTS records(
                code TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL, user_id BIGINT NOT NULL,
                source TEXT NOT NULL, category TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL,
                status TEXT NOT NULL, payload TEXT NOT NULL,
                retired_user INTEGER NOT NULL DEFAULT 0, retired_archive INTEGER NOT NULL DEFAULT 0)""",
            """CREATE TABLE IF NOT EXISTS message_refs(
                code TEXT NOT NULL, side TEXT NOT NULL, chat_id BIGINT NOT NULL,
                message_id BIGINT NOT NULL, sent_at DOUBLE PRECISION NOT NULL,
                deleted INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(side,chat_id,message_id))""",
            """CREATE TABLE IF NOT EXISTS people(
                user_id BIGINT PRIMARY KEY, name TEXT NOT NULL, username TEXT,
                first_seen DOUBLE PRECISION NOT NULL, last_seen DOUBLE PRECISION NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS confirmations(
                token TEXT PRIMARY KEY, code TEXT NOT NULL, target TEXT NOT NULL,
                expires DOUBLE PRECISION NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS inbox(
                update_id BIGINT PRIMARY KEY, payload TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                next_at DOUBLE PRECISION NOT NULL DEFAULT 0)""",
        ]
        with self.tx() as db:
            for statement in statements:
                db.execute(statement)

    def _connect(self):
        if self.persistent:
            import psycopg
            from psycopg.rows import dict_row
            if self.db is not None and not self.db.closed:
                try:
                    self.db.execute("SELECT 1")
                    return
                except psycopg.OperationalError:
                    self.db.close()
            self.db = psycopg.connect(
                self.database_url, autocommit=True, row_factory=dict_row,
                connect_timeout=10, options="-c statement_timeout=15000",
                prepare_threshold=None,
            )

    @contextmanager
    def tx(self):
        self._connect()
        if self.persistent:
            with self.db.transaction():
                yield Session(self.db, True)
        else:
            with self.db:
                yield Session(self.db, False)

    def rows(self, sql, values=()):
        with self.tx() as db:
            return [dict(row) for row in db.execute(sql, values).fetchall()]

    def one(self, sql, values=()):
        rows = self.rows(sql, values)
        return rows[0] if rows else None

    def write(self, sql, values=()):
        with self.tx() as db:
            db.execute(sql, values)

    def seed_aliases(self, defaults, imported):
        from core import alias_user_id, normalize_alias
        with self.tx() as db:
            already = db.execute("SELECT value FROM settings_meta WHERE key='aliases_v13'").fetchone()
            if already:
                return
            for uid, label in defaults.items():
                db.execute(
                    "INSERT INTO user_aliases VALUES (?,?) ON CONFLICT(user_id) DO NOTHING",
                    (alias_user_id(uid), normalize_alias(label)),
                )
            for uid, label in imported.items():
                db.execute(
                    "INSERT INTO user_aliases VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET label=excluded.label",
                    (alias_user_id(uid), normalize_alias(label)),
                )
            db.execute("INSERT INTO settings_meta VALUES ('aliases_v13','imported')")

    def alias_for(self, user_id):
        row = self.one("SELECT label FROM user_aliases WHERE user_id=?", (user_id,))
        return row["label"] if row else None

    def all_aliases(self):
        return {row["user_id"]: row["label"] for row in self.rows("SELECT * FROM user_aliases ORDER BY user_id")}

    def set_alias(self, user_id, label):
        from core import alias_user_id, normalize_alias
        self.write(
            "INSERT INTO user_aliases VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET label=excluded.label",
            (alias_user_id(user_id), normalize_alias(label)),
        )

    def remove_alias(self, user_id):
        from core import alias_user_id
        self.write("DELETE FROM user_aliases WHERE user_id=?", (alias_user_id(user_id),))

    def replace_aliases(self, aliases):
        from core import alias_user_id, normalize_alias
        validated = [(alias_user_id(uid), normalize_alias(label)) for uid, label in aliases.items()]
        with self.tx() as db:
            db.execute("DELETE FROM user_aliases")
            for pair in validated:
                db.execute("INSERT INTO user_aliases VALUES (?,?)", pair)

    def aliases_json(self):
        return json.dumps(
            {str(uid): label for uid, label in self.all_aliases().items()},
            ensure_ascii=False, separators=(",", ":"),
        )

    def remember_channel(self, chat_id, title):
        self.write(
            "INSERT INTO channels VALUES (?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title",
            (chat_id, title),
        )

    def channels(self):
        return self.rows("SELECT * FROM channels ORDER BY title")

    def seen_update(self, update_id):
        return self.one("SELECT id FROM updates WHERE id=?", (update_id,)) is not None

    def mark_update(self, update_id):
        with self.tx() as db:
            db.execute(
                "INSERT INTO updates VALUES (?,?) ON CONFLICT(id) DO NOTHING",
                (update_id, time.time()),
            )
            db.execute("DELETE FROM updates WHERE received_at<?", (time.time() - 172800,))

    def accept_update(self, update_id, payload):
        with self.tx() as db:
            inserted = db.execute(
                "INSERT INTO inbox(update_id,payload) VALUES (?,?) ON CONFLICT(update_id) DO NOTHING",
                (update_id, json.dumps(payload, ensure_ascii=False)),
            ).rowcount
        return bool(inserted)

    def inbox_next(self):
        return self.one(
            "SELECT * FROM inbox WHERE state='pending' AND next_at<=? ORDER BY update_id LIMIT 1",
            (time.time(),),
        )

    def inbox_done(self, update_id):
        self.write("UPDATE inbox SET state='done' WHERE update_id=?", (update_id,))

    def inbox_retry(self, update_id, attempts):
        self.write(
            "UPDATE inbox SET attempts=attempts+1,next_at=?,state=? WHERE update_id=?",
            (time.time() + min(300, 5 * 2**min(attempts, 6)),
             "failed" if attempts >= 11 else "pending", update_id),
        )

    def inbox_delay(self):
        row = self.one("SELECT MIN(next_at) AS due FROM inbox WHERE state='pending'")
        return None if row["due"] is None else max(0.1, row["due"] - time.time())

    def enqueue(self, chat_id, kind, file_id, body, extra=None):
        self.write(
            "INSERT INTO outbox(chat_id,kind,file_id,body,extra) VALUES (?,?,?,?,?)",
            (chat_id, kind, file_id, body, json.dumps(extra or {}, ensure_ascii=False)),
        )

    def next_job(self):
        return self.one("""
            SELECT * FROM outbox o WHERE attempts<12 AND next_at<=? AND NOT EXISTS(
                SELECT 1 FROM outbox p WHERE p.chat_id=o.chat_id AND p.id<o.id AND p.attempts<12
            ) ORDER BY id LIMIT 1
        """, (time.time(),))

    def archive_delay(self):
        row = self.one("""
            SELECT MIN(next_at) AS due FROM outbox o WHERE attempts<12 AND NOT EXISTS(
                SELECT 1 FROM outbox p WHERE p.chat_id=o.chat_id AND p.id<o.id AND p.attempts<12
            )
        """)
        return None if row["due"] is None else max(0.2, row["due"] - time.time())

    def done(self, job_id):
        self.write("DELETE FROM outbox WHERE id=?", (job_id,))

    def retry(self, job_id, delay):
        self.write(
            "UPDATE outbox SET attempts=attempts+1,next_at=? WHERE id=?",
            (time.time() + delay, job_id),
        )

    def pending(self):
        return self.one("SELECT COUNT(*) AS n FROM outbox")["n"]

    def archive_failures(self):
        return self.rows("SELECT * FROM outbox WHERE attempts>=12 ORDER BY id LIMIT 20")

    def retry_archives(self):
        self.write("UPDATE outbox SET attempts=0,next_at=0")

    def observe_person(self, user_id, name, username):
        now = time.time()
        self.write(
            """INSERT INTO people VALUES (?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
               name=excluded.name,username=excluded.username,last_seen=excluded.last_seen""",
            (user_id, name, username, now, now),
        )

    def unlisted_people(self):
        return self.rows(
            """SELECT p.* FROM people p LEFT JOIN user_aliases a ON p.user_id=a.user_id
               WHERE a.user_id IS NULL ORDER BY p.last_seen DESC LIMIT 40"""
        )

    def create_record(self, request_key, user_id, source, category, payload, status):
        code = ("D" if category == "download" else "A") + uuid.uuid4().hex[:11].upper()
        with self.tx() as db:
            db.execute(
                """INSERT INTO records(
                    code,request_key,user_id,source,category,created_at,status,payload
                ) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(request_key) DO NOTHING""",
                (code, request_key, user_id, source, category, time.time(), status,
                 json.dumps(payload, ensure_ascii=False)),
            )
            row = db.execute("SELECT * FROM records WHERE request_key=?", (request_key,)).fetchone()
        return dict(row)

    def record(self, code):
        return self.one("SELECT * FROM records WHERE code=?", (code,))

    def records(self, user_id=None):
        if user_id is None:
            return self.rows("SELECT * FROM records ORDER BY created_at DESC LIMIT 25")
        return self.rows(
            "SELECT * FROM records WHERE user_id=? ORDER BY created_at DESC LIMIT 25", (user_id,),
        )

    def set_status(self, code, status):
        self.write("UPDATE records SET status=? WHERE code=?", (status, code))

    def stage_jobs(self, code, jobs):
        """Queue a received contribution and metadata atomically and idempotently."""
        with self.tx() as db:
            row = db.execute("SELECT status FROM records WHERE code=?", (code,)).fetchone()
            if not row or row["status"] != "new":
                return
            for job in jobs:
                extra = {**job.get("extra", {}), "_record": code}
                db.execute(
                    "INSERT INTO outbox(chat_id,kind,file_id,body,extra) VALUES (?,?,?,?,?)",
                    (job["chat_id"], job["kind"], job.get("file_id"), job.get("body", ""),
                     json.dumps(extra, ensure_ascii=False)),
                )
            db.execute("UPDATE records SET status='received' WHERE code=?", (code,))

    def update_payload(self, code, payload):
        self.write(
            "UPDATE records SET payload=? WHERE code=?",
            (json.dumps(payload, ensure_ascii=False), code),
        )

    def queued_records(self):
        return self.rows("SELECT * FROM records WHERE status='queued' ORDER BY created_at")

    def interrupted_records(self):
        return self.rows("SELECT * FROM records WHERE status='processing' ORDER BY created_at")

    def add_ref(self, code, side, chat_id, message_id, sent_at=None):
        self.write(
            """INSERT INTO message_refs(code,side,chat_id,message_id,sent_at) VALUES (?,?,?,?,?)
               ON CONFLICT(side,chat_id,message_id) DO NOTHING""",
            (code, side, chat_id, message_id, sent_at or time.time()),
        )

    def refs(self, code, target):
        if target == "both":
            return self.rows("SELECT * FROM message_refs WHERE code=? AND deleted=0 ORDER BY side,message_id", (code,))
        side = "archive" if target == "archive" else "user"
        return self.rows(
            "SELECT * FROM message_refs WHERE code=? AND side=? AND deleted=0 ORDER BY message_id", (code, side),
        )

    def mark_deleted(self, ref):
        self.write(
            "UPDATE message_refs SET deleted=1 WHERE side=? AND chat_id=? AND message_id=?",
            (ref["side"], ref["chat_id"], ref["message_id"]),
        )

    def retire(self, code, target):
        with self.tx() as db:
            if target in ("user", "both"):
                db.execute("UPDATE records SET retired_user=1 WHERE code=?", (code,))
            if target in ("archive", "both"):
                db.execute("UPDATE records SET retired_archive=1 WHERE code=?", (code,))
                for row in db.execute("SELECT id,extra FROM outbox").fetchall():
                    if json.loads(row["extra"]).get("_record") == code:
                        db.execute("DELETE FROM outbox WHERE id=?", (row["id"],))

    def confirmation(self, code, target):
        token = uuid.uuid4().hex[:20]
        with self.tx() as db:
            db.execute("DELETE FROM confirmations WHERE expires<?", (time.time(),))
            db.execute(
                "INSERT INTO confirmations VALUES (?,?,?,?)", (token, code, target, time.time() + 300),
            )
        return token

    def consume_confirmation(self, token):
        with self.tx() as db:
            row = db.execute(
                "SELECT * FROM confirmations WHERE token=? AND expires>?", (token, time.time()),
            ).fetchone()
            if row is None:
                return None
            removed = db.execute("DELETE FROM confirmations WHERE token=?", (token,)).rowcount
        return dict(row) if removed else None

    def close(self):
        if self.db is not None:
            self.db.close()
