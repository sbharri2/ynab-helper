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
  raw_summary TEXT,
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
  quiet_until TIMESTAMP,
  last_turns_json TEXT             -- JSON list of last N (role, content) tuples
                                   -- used by Phase 3.5 AI-mediated chat layer
                                   -- to provide multi-turn context to qwen3:32b
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

-- Phase 5 per-user notification preferences. Designed for multi-user from day
-- one — every report loops users × prefs. Allison's row is created when she
-- /starts the bot (Phase 6); Steven's is created on first contact.
CREATE TABLE IF NOT EXISTS user_pref (
  user_id TEXT PRIMARY KEY,
  receives_per_txn INTEGER NOT NULL DEFAULT 1,
  receives_daily INTEGER NOT NULL DEFAULT 1,
  receives_weekly INTEGER NOT NULL DEFAULT 1,
  quiet_hours TEXT NOT NULL DEFAULT '22:00-07:00',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Phase 2 ledger tables. The new YNAB-equivalent data model — SQLite as the
-- system of record. Bootstrapped from the YNAB history dump and updated by
-- bot/ingest.py (Phase 3) from incoming email signals. Mirrored to YNAB
-- during the transition, source of truth after Phase 7 cutover.
CREATE TABLE IF NOT EXISTS account (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  type TEXT NOT NULL CHECK (type IN ('checking','savings','credit_card','tracking','cash','line_of_credit','other_asset','other_liability')),
  on_budget INTEGER NOT NULL DEFAULT 1,
  ynab_account_id TEXT,
  closed INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS category_group (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  sort_order INTEGER DEFAULT 0,
  hidden INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS category (
  id TEXT PRIMARY KEY,
  group_id TEXT NOT NULL REFERENCES category_group(id),
  name TEXT NOT NULL,
  ynab_category_id TEXT,
  hidden INTEGER DEFAULT 0,
  is_spending INTEGER DEFAULT 1,
  goal_kind TEXT,
  goal_target_cents INTEGER,
  goal_day INTEGER
);

CREATE TABLE IF NOT EXISTS month_category (
  month TEXT NOT NULL,
  category_id TEXT NOT NULL REFERENCES category(id),
  budgeted_cents INTEGER NOT NULL DEFAULT 0,
  activity_cents INTEGER NOT NULL DEFAULT 0,
  available_cents INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (month, category_id)
);

CREATE TABLE IF NOT EXISTS ledger_txn (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id TEXT NOT NULL REFERENCES account(id),
  posted_date DATE NOT NULL,
  amount_cents INTEGER NOT NULL,
  payee TEXT,
  memo TEXT,
  category_id TEXT REFERENCES category(id),
  cleared TEXT NOT NULL DEFAULT 'uncleared' CHECK (cleared IN ('uncleared','cleared','reconciled')),
  source_signal TEXT,
  source_email_id TEXT,
  ynab_txn_id TEXT,
  dedupe_key TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (ynab_txn_id)
);
CREATE INDEX IF NOT EXISTS idx_ledger_dedupe ON ledger_txn(dedupe_key, posted_date);
CREATE INDEX IF NOT EXISTS idx_ledger_account_date ON ledger_txn(account_id, posted_date);
CREATE INDEX IF NOT EXISTS idx_ledger_category ON ledger_txn(category_id, posted_date);
CREATE INDEX IF NOT EXISTS idx_ledger_payee ON ledger_txn(payee);

CREATE TABLE IF NOT EXISTS ledger_signal (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ledger_txn_id INTEGER NOT NULL REFERENCES ledger_txn(id),
  signal_kind TEXT NOT NULL,
  email_id TEXT NOT NULL,
  parsed_payload TEXT,
  received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (signal_kind, email_id)
);

CREATE TABLE IF NOT EXISTS account_balance_observed (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id TEXT NOT NULL REFERENCES account(id),
  as_of_date DATE NOT NULL,
  balance_cents INTEGER NOT NULL,
  source_email_id TEXT,
  observed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (account_id, as_of_date)
);

-- Phase 0 observation table. Captures raw email bodies from senders the bot
-- doesn't yet parse, so we can sample real templates before writing parsers.
-- No business logic reads from this; it's purely a sampling sidecar.
CREATE TABLE IF NOT EXISTS raw_email_sample (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email_id TEXT NOT NULL UNIQUE,
  account_email TEXT NOT NULL,
  sender TEXT NOT NULL,
  sender_label TEXT,
  subject TEXT,
  date_header TEXT,
  internal_date TEXT,
  snippet TEXT,
  body_text TEXT,
  body_html TEXT,
  has_attachments INTEGER DEFAULT 0,
  parser_proposal TEXT,
  reviewed INTEGER DEFAULT 0,
  inserted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_raw_email_sender
  ON raw_email_sample(sender_label, inserted_at);

-- Cached "what kind of business is this payee?" lookups. Used by the
-- categorizer to give qwen3:32b useful context for payees with no
-- historical priors. Each payee is looked up at most once; a refresh
-- only happens if the cached row gets manually deleted or expires.
CREATE TABLE IF NOT EXISTS payee_intel (
  payee_norm TEXT PRIMARY KEY,        -- lowercased + stripped payee name
  payee_raw TEXT NOT NULL,            -- original payee for debugging
  description TEXT,                    -- one-sentence "what is this" blurb
  source TEXT NOT NULL,                -- 'llm_knowledge' | 'duckduckgo' | 'none'
  confidence REAL DEFAULT 0,           -- 0-1 self-reported by LLM
  fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
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
        _migrate(con)


def _migrate(con) -> None:
    """Run additive ALTER TABLE migrations. Safe to call on a fresh DB.

    Each migration checks current columns/indexes via PRAGMA and only acts
    when needed. Order matters when a new column references another new
    column, but for now all migrations are independent.
    """
    bot_conv_cols = {
        r[1] for r in con.execute("PRAGMA table_info(bot_conversation)")
    }
    if "last_turns_json" not in bot_conv_cols:
        con.execute("ALTER TABLE bot_conversation ADD COLUMN last_turns_json TEXT")
    if "last_asked_message_id" not in bot_conv_cols:
        # Telegram message_id of the most recent in-flight question. Used to
        # strip the inline keyboard off the old message before pushing a new
        # one — otherwise the chat shows multiple "open" questions and the
        # user can't tell which one they're answering.
        con.execute(
            "ALTER TABLE bot_conversation ADD COLUMN last_asked_message_id INTEGER"
        )

    account_cols = {
        r[1] for r in con.execute("PRAGMA table_info(account)")
    }
    if "balance_cents" not in account_cols:
        # YNAB-reported balance at import time. Daily summary uses this until
        # bank balance-summary emails populate account_balance_observed.
        con.execute("ALTER TABLE account ADD COLUMN balance_cents INTEGER DEFAULT 0")
    if "cleared_balance_cents" not in account_cols:
        con.execute("ALTER TABLE account ADD COLUMN cleared_balance_cents INTEGER DEFAULT 0")


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


# ---------------------------------------------------------------------------
# Phase 2 ledger read API
# ---------------------------------------------------------------------------

def list_categories_for_spending(db_path: Path | str) -> list[dict]:
    """The category list to feed the LLM categorizer.

    Excludes:
      - Categories with is_spending=0 (Credit Card Payments, scheduled bills,
        named-goal categories like "Piano Lessons (1st)")
      - Hidden / deleted categories
      - The internal "Uncategorized" / "Inflow: Ready to Assign" pair

    Each row has the joined group name so the LLM has the YNAB hierarchy.
    """
    with connect(db_path) as con:
        rows = con.execute(
            """SELECT c.id, c.name, c.group_id, g.name AS group_name
               FROM category c
               JOIN category_group g ON g.id = c.group_id
               WHERE c.hidden = 0
                 AND c.is_spending = 1
                 AND g.hidden = 0
               ORDER BY g.sort_order, g.name, c.name"""
        ).fetchall()
        return [dict(r) for r in rows]


def get_category_priors_for_payee(
    db_path: Path | str,
    payee: str,
    *,
    limit_history_days: int = 730,
    top_n: int = 5,
) -> list[dict]:
    """How has this payee been categorized historically?

    Returns top-N categories by transaction count for the given payee over
    the lookback window, with percent share. Used to bias the LLM categorizer.

    Matches payees case-insensitively and via LIKE on the leading word, so
    "Amazon" matches "Amazon.com", "Amazon Marketplace", etc.
    """
    norm = (payee or "").strip().lower()
    if not norm:
        return []
    first_word = norm.split()[0]
    pattern = f"{first_word}%"
    with connect(db_path) as con:
        # JOIN (not LEFT JOIN) + is_spending=1 filter so we never surface
        # CC-payment buckets, scheduled-bill envelopes, or named-goal
        # categories as priors. Without this filter the Venmo priors
        # would include "Inflow: Ready to Assign", "Allison Personal
        # Savings", "Sewing Class", "Piano Lessons (1st)" — categories
        # the LLM is told to refuse, which were then surfacing as
        # keyboard buttons. Confusing.
        #
        # UNION ALL with pending_txn (status='categorized') so that
        # categorizations the user has JUST made — but haven't yet been
        # back-filled into ledger_txn — bias the next prompt for the
        # same payee. Without this, if the user marks Tennfold $34.94
        # as Gifts, the next Tennfold prompt would still get whatever
        # priors the ledger has (possibly none). Fresh choices count.
        rows = con.execute(
            """SELECT category_id,
                      category_name,
                      SUM(n) AS n,
                      SUM(total_cents) AS total_cents
               FROM (
                 SELECT t.category_id,
                        c.name AS category_name,
                        1 AS n,
                        t.amount_cents AS total_cents
                 FROM ledger_txn t
                 JOIN category c ON c.id = t.category_id
                 WHERE LOWER(t.payee) LIKE ?
                   AND c.is_spending = 1
                   AND t.posted_date >= date('now', ?)
                 UNION ALL
                 SELECT p.chosen_category AS category_id,
                        c.name AS category_name,
                        1 AS n,
                        p.amount_cents AS total_cents
                 FROM pending_txn p
                 JOIN category c ON c.id = p.chosen_category
                 WHERE p.status = 'categorized'
                   AND LOWER(p.payee) LIKE ?
                   AND c.is_spending = 1
               )
               GROUP BY category_id, category_name
               ORDER BY n DESC
               LIMIT ?""",
            (pattern, f"-{int(limit_history_days)} days", pattern, top_n),
        ).fetchall()
    if not rows:
        return []
    total = sum(r["n"] for r in rows)
    return [
        {
            "category_id": r["category_id"],
            "category_name": r["category_name"],
            "count": r["n"],
            "total_cents": r["total_cents"] or 0,
            "pct": round(r["n"] / total, 3),
        }
        for r in rows
    ]


def get_or_create_user_pref(db_path: Path | str, user_id: str) -> dict:
    """Ensure a user_pref row exists for `user_id`, return it.

    Defaults match Phase 5/6 plan: new users get daily + weekly + per-txn DMs
    on, with the default quiet window. Tune later via the agent's `/me` tool.
    """
    with connect(db_path) as con:
        existing = con.execute(
            "SELECT * FROM user_pref WHERE user_id = ?", (user_id,),
        ).fetchone()
        if existing:
            return dict(existing)
        con.execute(
            "INSERT INTO user_pref (user_id) VALUES (?)", (user_id,),
        )
        row = con.execute(
            "SELECT * FROM user_pref WHERE user_id = ?", (user_id,),
        ).fetchone()
        return dict(row)


def list_recipients_for_period(
    db_path: Path | str, period: str,
) -> list[dict]:
    """Return user_pref rows for users opted in to a notification period.

    `period` is one of: 'daily', 'weekly', 'per_txn'. Each returned row has
    user_id + their quiet_hours setting.
    """
    col = {
        "daily": "receives_daily",
        "weekly": "receives_weekly",
        "per_txn": "receives_per_txn",
    }.get(period)
    if col is None:
        raise ValueError(f"unknown period: {period}")
    with connect(db_path) as con:
        rows = con.execute(
            f"SELECT user_id, quiet_hours FROM user_pref WHERE {col} = 1",
        ).fetchall()
        return [dict(r) for r in rows]


def record_observed_balance(
    db_path: Path | str,
    *,
    account_id: str,
    as_of_date: date,
    balance_cents: int,
    source_email_id: str | None = None,
) -> int | None:
    """Record a bank-reported balance for an account on a given date.

    Idempotent on (account_id, as_of_date) — only one balance per day per
    account. Returns the row id, or None if already filed.
    """
    with connect(db_path) as con:
        try:
            cur = con.execute(
                """INSERT INTO account_balance_observed
                   (account_id, as_of_date, balance_cents, source_email_id)
                   VALUES (?, ?, ?, ?)""",
                (account_id, as_of_date, balance_cents, source_email_id),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None


# ---------------------------------------------------------------------------
# Phase 0 raw sample helper
# ---------------------------------------------------------------------------

def insert_raw_email_sample(
    db_path: Path | str,
    *,
    email_id: str,
    account_email: str,
    sender: str,
    sender_label: str | None,
    subject: str | None,
    date_header: str | None,
    internal_date: str | None,
    snippet: str | None,
    body_text: str | None,
    body_html: str | None,
    has_attachments: bool = False,
) -> int | None:
    """Idempotent on email_id. Returns new row id or None if already filed."""
    with connect(db_path) as con:
        try:
            cur = con.execute(
                """
                INSERT INTO raw_email_sample
                  (email_id, account_email, sender, sender_label, subject,
                   date_header, internal_date, snippet, body_text, body_html,
                   has_attachments)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (email_id, account_email, sender, sender_label, subject,
                 date_header, internal_date, snippet, body_text, body_html,
                 1 if has_attachments else 0),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None
