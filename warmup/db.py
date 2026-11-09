from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA = """
create table if not exists meta(k text primary key, v text);
create table if not exists threads(
  id integer primary key, subject text, a text, b text,
  total_turns integer, created_at integer);
create table if not exists messages(
  id integer primary key, thread_id integer, turn integer,
  sender text, recipient text, message_id text unique,
  subject text, body text, sent_at integer,
  arrived_at integer, placement text, source text);
create table if not exists jobs(
  id integer primary key, due_at integer, kind text,
  thread_id integer, sender text, recipient text, turn integer,
  attempts integer default 0, status text default 'pending', error text,
  payload text, started_at integer);
create table if not exists trail_state(
  trail_id text primary key, status text, thread_id integer,
  last_turn integer default 0, updated_at integer);
create index if not exists jobs_due on jobs(status, due_at);
create index if not exists msgs_pending on messages(arrived_at, sent_at);
"""

# columns added after the first release: (table, column, type)
MIGRATIONS = [
    ("jobs", "payload", "text"),
    ("jobs", "started_at", "integer"),
    ("messages", "source", "text"),
    ("jobs", "trail_id", "text"),
    ("threads", "trail_id", "text"),
]


class DB:
    def __init__(self, path: str):
        # timeout = busy timeout in seconds, so a concurrent `status` read never errors out a tick
        self.c = sqlite3.connect(path, timeout=10)
        self.c.row_factory = sqlite3.Row
        if path != ":memory:":
            self.c.execute("pragma journal_mode=wal")
        self.c.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        for table, col, typ in MIGRATIONS:
            cols = {r["name"] for r in self.c.execute(f"pragma table_info({table})")}
            if col not in cols:
                self.c.execute(f"alter table {table} add column {col} {typ}")
        self.c.commit()

    def get(self, k: str, default: str | None = None) -> str | None:
        r = self.c.execute("select v from meta where k=?", (k,)).fetchone()
        return r["v"] if r else default

    def set(self, k: str, v: str) -> None:
        self.c.execute("insert or replace into meta values(?,?)", (k, v))
        self.c.commit()

    def unset(self, k: str) -> None:
        self.c.execute("delete from meta where k=?", (k,))
        self.c.commit()

    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self.c.execute(sql, args).fetchall()

    def x(self, sql: str, args: tuple = ()) -> int:
        cur = self.c.execute(sql, args)
        self.c.commit()
        return cur.lastrowid or 0

    def tx(self, stmts: list[tuple[str, tuple]]) -> list[int]:
        """Run several statements as one atomic transaction."""
        ids = []
        with self.c:
            for sql, args in stmts:
                ids.append(self.c.execute(sql, args).lastrowid or 0)
        return ids

    def backup(self, dest_dir: str, keep: int = 14) -> Path:
        """Consistent online copy (safe while `run` is writing). Verified, chmod 600, old ones pruned."""
        d = Path(dest_dir)
        d.mkdir(parents=True, exist_ok=True)
        dest = d / f"warmup-{datetime.now():%Y%m%d-%H%M%S}.db"
        out = sqlite3.connect(dest)
        try:
            self.c.backup(out)
            ok = out.execute("pragma integrity_check").fetchone()[0]
        finally:
            out.close()
        if ok != "ok":
            dest.unlink(missing_ok=True)
            raise RuntimeError(f"backup failed integrity check: {ok}")
        os.chmod(dest, 0o600)  # holds message bodies
        for old in sorted(d.glob("warmup-*.db"))[:-keep] if keep > 0 else []:
            old.unlink()
        return dest
