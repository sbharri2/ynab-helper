"""SQLite storage layer for ynab-helper.

All schema and CRUD lives here. No other module reads/writes the DB directly.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator

# Python 3.12 deprecated the default date/datetime adapters and converters that
# `detect_types=PARSE_DECLTYPES` relies on. Register explicit ISO-8601 ones now
# so the warnings don't pollute every test run downstream.
sqlite3.register_adapter(date, lambda d: d.isoformat())
sqlite3.register_adapter(datetime, lambda dt: dt.isoformat())
sqlite3.register_converter("DATE", lambda b: date.fromisoformat(b.decode()))
sqlite3.register_converter("TIMESTAMP", lambda b: datetime.fromisoformat(b.decode()))

SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_order (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('amazon','venmo')),
  external_id TEXT,
  email_id TEXT NOT NULL UNIQUE,
  order_date DATE NOT NULL,
  total_cents INTEGER NOT NULL,
  raw_summary TEXT,
  raw_payload TEXT,
  suggested_category TEXT,
  suggested_confidence REAL,
  chosen_category TEXT,
  chosen_at TIMESTAMP,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','categorized','matched','expired')),
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pending_txn (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  ynab_txn_id TEXT NOT NULL UNIQUE,
  ynab_account_id TEXT,
  payee TEXT,
  amount_cents INTEGER NOT NULL,
  txn_date DATE NOT NULL,
  memo TEXT,
  suggested_category TEXT,
  chosen_category TEXT,
  chosen_at TIMESTAMP,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','categorized','skipped')),
  digest_run_id INTEGER,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS matched_charge (
  pending_order_id INTEGER REFERENCES pending_order(id),
  ynab_txn_id TEXT NOT NULL,
  matched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (pending_order_id, ynab_txn_id)
);

CREATE TABLE IF NOT EXISTS bot_conversation (
  chat_id INTEGER PRIMARY KEY,
  user_id TEXT NOT NULL,
  last_asked_kind TEXT CHECK (last_asked_kind IN ('order','txn')),
  last_asked_id INTEGER,
  last_action_at TIMESTAMP,
  quiet_until TIMESTAMP
);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  event TEXT NOT NULL,
  details TEXT
);

CREATE INDEX IF NOT EXISTS idx_pending_order_status
  ON pending_order(status, source, user_id);
CREATE INDEX IF NOT EXISTS idx_pending_txn_status
  ON pending_txn(status, user_id);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
"""


def _utcnow() -> datetime:
    """Timezone-aware UTC timestamp (replaces deprecated datetime.utcnow())."""
    return datetime.now(timezone.utc)


@contextmanager
def connect(db_path: Path | str) -> Iterator[sqlite3.Connection]:
    con = sqlite3.connect(str(db_path), detect_types=sqlite3.PARSE_DECLTYPES)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db(db_path: Path | str) -> None:
    with connect(db_path) as con:
        con.executescript(SCHEMA)


def insert_pending_order(
    db_path: Path | str,
    *,
    user_id: str,
    source: str,
    external_id: str | None,
    email_id: str,
    order_date: date,
    total_cents: int,
    raw_summary: str,
    raw_payload: dict[str, Any],
) -> int:
    """Idempotent on email_id - returns existing id if already inserted."""
    with connect(db_path) as con:
        existing = con.execute(
            "SELECT id FROM pending_order WHERE email_id = ?", (email_id,)
        ).fetchone()
        if existing:
            return existing["id"]
        cur = con.execute(
            """
            INSERT INTO pending_order
              (user_id, source, external_id, email_id, order_date,
               total_cents, raw_summary, raw_payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, source, external_id, email_id, order_date,
             total_cents, raw_summary, json.dumps(raw_payload, default=str)),
        )
        return cur.lastrowid


def get_pending_orders(db_path: Path | str, *, status: str) -> list[dict]:
    with connect(db_path) as con:
        rows = con.execute(
            "SELECT * FROM pending_order WHERE status = ? ORDER BY id",
            (status,),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_order_categorized(
    db_path: Path | str, order_id: int, *, chosen_category: str
) -> None:
    with connect(db_path) as con:
        now = _utcnow()
        con.execute(
            """
            UPDATE pending_order
            SET chosen_category = ?, chosen_at = ?, status = 'categorized',
                updated_at = ?
            WHERE id = ?
            """,
            (chosen_category, now, now, order_id),
        )


def insert_pending_txn(
    db_path: Path | str,
    *,
    user_id: str,
    ynab_txn_id: str,
    ynab_account_id: str,
    payee: str,
    amount_cents: int,
    txn_date: date,
    memo: str,
) -> int | None:
    """Returns new id, or None if ynab_txn_id already exists."""
    with connect(db_path) as con:
        try:
            cur = con.execute(
                """
                INSERT INTO pending_txn
                  (user_id, ynab_txn_id, ynab_account_id, payee,
                   amount_cents, txn_date, memo)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, ynab_txn_id, ynab_account_id, payee,
                 amount_cents, txn_date, memo),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None


def list_unmatched_amazon_orders(db_path: Path | str) -> list[dict]:
    """Orders that have been categorized but not yet linked to a YNAB charge."""
    with connect(db_path) as con:
        rows = con.execute(
            """
            SELECT po.* FROM pending_order po
            LEFT JOIN matched_charge mc ON mc.pending_order_id = po.id
            WHERE po.source = 'amazon'
              AND po.status = 'categorized'
              AND mc.pending_order_id IS NULL
            """
        ).fetchall()
        return [dict(r) for r in rows]


def record_match(
    db_path: Path | str, *, pending_order_id: int, ynab_txn_id: str
) -> None:
    with connect(db_path) as con:
        con.execute(
            "INSERT OR IGNORE INTO matched_charge "
            "(pending_order_id, ynab_txn_id) VALUES (?, ?)",
            (pending_order_id, ynab_txn_id),
        )
        # Promote the order's status only when at least one charge is linked.
        con.execute(
            "UPDATE pending_order SET status = 'matched', updated_at = ? "
            "WHERE id = ?",
            (_utcnow(), pending_order_id),
        )


def audit(db_path: Path | str, event: str, details: dict[str, Any] | str = "") -> None:
    payload = json.dumps(details) if isinstance(details, dict) else str(details)
    with connect(db_path) as con:
        con.execute("INSERT INTO audit_log (event, details) VALUES (?, ?)",
                    (event, payload))
