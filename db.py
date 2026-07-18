"""SQLite state store for the orchestrator.

Single source of truth for routing (chat_id -> session_id + cwd), the inbox,
turn history and spend metrics. In the Phase 1 file-outbox design ONLY the
daemon process touches this DB, so all access happens on the daemon's event-loop
thread — no cross-process contention, no locks needed.
"""
import os
import sqlite3
from typing import Optional, List, Tuple

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS clients (
  chat_id       INTEGER PRIMARY KEY,
  username      TEXT,
  session_id    TEXT UNIQUE NOT NULL,        -- uuid4 we mint, passed to claude --session-id/--resume
  cwd           TEXT NOT NULL,               -- absolute per-client working dir (NEVER relocate)
  project_type  TEXT DEFAULT 'unknown',      -- 'pine' | 'mt5' | 'unknown'
  status        TEXT DEFAULT 'active',       -- 'active' | 'busy' | 'blocked' | 'capped'
  started       INTEGER DEFAULT 0,           -- 0 -> first turn uses --session-id, then 1 -> --resume
  spend_usd     REAL DEFAULT 0,
  last_activity TEXT,
  created_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS inbox (
  msg_id   INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id  INTEGER NOT NULL,
  text     TEXT,
  media    TEXT,                              -- JSON array of downloaded file paths
  status   TEXT DEFAULT 'queued',             -- 'queued' | 'done' | 'error'
  ts       TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_inbox_status ON inbox(status);

CREATE TABLE IF NOT EXISTS turns (
  turn_id    INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id    INTEGER NOT NULL,
  in_msg_id  INTEGER,
  session_id TEXT NOT NULL,
  status     TEXT DEFAULT 'in_flight',        -- 'in_flight' | 'delivered' | 'needs_review' | 'error'
  cost_usd   REAL DEFAULT 0,
  started    TEXT DEFAULT (datetime('now')),
  finished   TEXT
);
CREATE INDEX IF NOT EXISTS idx_turns_status ON turns(status);

CREATE TABLE IF NOT EXISTS metrics (
  ts      TEXT DEFAULT (datetime('now')),
  chat_id INTEGER,
  kind    TEXT,
  value   REAL
);
"""


class Db:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---- clients / routing ----
    def get_client(self, chat_id: int) -> Optional[sqlite3.Row]:
        cur = self.conn.execute("SELECT * FROM clients WHERE chat_id=?", (chat_id,))
        return cur.fetchone()

    def create_client(self, chat_id: int, username: Optional[str], session_id: str, cwd: str):
        self.conn.execute(
            "INSERT INTO clients (chat_id, username, session_id, cwd, last_activity) "
            "VALUES (?,?,?,?, datetime('now'))",
            (chat_id, username, session_id, cwd),
        )
        self.conn.commit()

    def mark_started(self, chat_id: int):
        self.conn.execute("UPDATE clients SET started=1 WHERE chat_id=?", (chat_id,))
        self.conn.commit()

    def set_status(self, chat_id: int, status: str):
        self.conn.execute("UPDATE clients SET status=? WHERE chat_id=?", (status, chat_id))
        self.conn.commit()

    def set_project_type(self, chat_id: int, ptype: str):
        self.conn.execute("UPDATE clients SET project_type=? WHERE chat_id=?", (ptype, chat_id))
        self.conn.commit()

    def bump_spend(self, chat_id: int, delta: float) -> float:
        self.conn.execute(
            "UPDATE clients SET spend_usd = spend_usd + ?, last_activity=datetime('now') WHERE chat_id=?",
            (delta, chat_id),
        )
        self.conn.commit()
        row = self.get_client(chat_id)
        return row["spend_usd"] if row else 0.0

    def all_client_dirs(self) -> List[Tuple[int, str]]:
        cur = self.conn.execute("SELECT chat_id, cwd FROM clients")
        return [(r["chat_id"], r["cwd"]) for r in cur.fetchall()]

    # ---- inbox ----
    def insert_inbox(self, chat_id: int, text: str, media_json: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO inbox (chat_id, text, media) VALUES (?,?,?)",
            (chat_id, text, media_json),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_inbox(self, msg_id: int) -> Optional[sqlite3.Row]:
        cur = self.conn.execute("SELECT * FROM inbox WHERE msg_id=?", (msg_id,))
        return cur.fetchone()

    def set_inbox_status(self, msg_id: int, status: str):
        self.conn.execute("UPDATE inbox SET status=? WHERE msg_id=?", (status, msg_id))
        self.conn.commit()

    def queued_inbox(self) -> List[sqlite3.Row]:
        cur = self.conn.execute("SELECT * FROM inbox WHERE status='queued' ORDER BY msg_id ASC")
        return cur.fetchall()

    # ---- turns ----
    def open_turn(self, chat_id: int, in_msg_id: int, session_id: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO turns (chat_id, in_msg_id, session_id) VALUES (?,?,?)",
            (chat_id, in_msg_id, session_id),
        )
        self.conn.commit()
        return cur.lastrowid

    def close_turn(self, turn_id: int, status: str, cost: float):
        self.conn.execute(
            "UPDATE turns SET status=?, cost_usd=?, finished=datetime('now') WHERE turn_id=?",
            (status, cost, turn_id),
        )
        self.conn.commit()

    def reset_in_flight(self):
        """On startup, any turn still 'in_flight' died with the process — flag for review."""
        self.conn.execute("UPDATE turns SET status='needs_review' WHERE status='in_flight'")
        self.conn.commit()

    # ---- metrics ----
    def add_metric(self, chat_id: Optional[int], kind: str, value: float):
        self.conn.execute("INSERT INTO metrics (chat_id, kind, value) VALUES (?,?,?)", (chat_id, kind, value))
        self.conn.commit()


def expand(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))
