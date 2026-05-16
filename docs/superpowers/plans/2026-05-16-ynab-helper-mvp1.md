# YNAB Helper MVP-1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a working Telegram bot that pre-categorizes Amazon and Venmo purchases from Gmail emails the moment they arrive, sends a 9am daily digest of all other YNAB transactions, applies categories to YNAB via API, and runs on a single home desktop. One user (Steven). MVP-2 (wife's Gmail) and MVP-3 (weekly digest) are separate plans.

**Architecture:** Six components — `gmail_watcher` (scheduled task), `ynab_watcher` (scheduled task), `telegram_bot` (long-running service), `parsers/` and `matcher.py` (pure functions called by watchers), `categorizer.py` (Ollama HTTP). All state in a single SQLite file. Email arrival is the real-time trigger for Amazon/Venmo; YNAB API poll is the trigger for everything else. Local Ollama on the user's RTX 5090 supplies category suggestions. See `docs/superpowers/specs/2026-05-16-ynab-helper-design.md` for the full design and `docs/superpowers/specs/2026-05-16-parsing-knowledge.md` for the parsing patterns we're inheriting.

**Tech Stack:**
- Python 3.12+
- SQLite (single file, no server)
- `python-telegram-bot` v21+ (async, long-polling)
- `google-api-python-client` + `google-auth-oauthlib` (Gmail)
- `ynab` (official YNAB Python SDK)
- `httpx` (Ollama HTTP API)
- `beautifulsoup4` + `lxml` (HTML email parsing)
- `pydantic-settings` (typed config from yaml + env)
- `python-dotenv`
- `pytest` + `pytest-asyncio` (testing)
- Ollama (whatever model fits on the 5090 — default `qwen2.5:14b`)
- Windows Task Scheduler + (optional) NSSM for the long-running bot service

---

## Pre-flight: Validate parsing assumptions

### Task 0: Run email inspection and confirm parsing assumptions

This must run before any parser code is written. Without real email fixtures, the parser tests are guessing.

**Files:**
- Read: `docs/superpowers/specs/2026-05-16-parsing-knowledge.md`
- Run: `scripts/reauth_gmail.py`, `scripts/inspect_receipts.py`
- Produces: `receipt-inspection.md`, `tests/fixtures/amazon_emails/*.html`, `tests/fixtures/venmo_emails/*.html`

- [ ] **Step 1: Run OAuth re-auth** (user must approve in browser)

```powershell
python scripts\reauth_gmail.py
```

Expected: a browser window opens, user signs in as `sbharri2@gmail.com`, approves `gmail.modify` + `gmail.readonly`. Script prints `Token written to: <path>` and `Refresh-able: True`.

- [ ] **Step 2: Run inspection script**

```powershell
python scripts\inspect_receipts.py
```

Expected: console output showing per-sender counts and pattern verification, markdown report written to `receipt-inspection.md`, HTML/text fixtures saved under `tests/fixtures/amazon_emails/` and `tests/fixtures/venmo_emails/`.

- [ ] **Step 3: Review report against parsing-knowledge assumptions**

Open `receipt-inspection.md`. For each assumption in `parsing-knowledge.md`, confirm:
- Amazon: which sender(s) carry order confirmations (expected: `auto-confirm@amazon.com`)
- Amazon: order ID regex `\d{3}-\d{7}-\d{7}` present in N/N order-confirmation emails
- Amazon: "Order placed" phrase present
- Amazon: "Total" or "Order Total" phrase present
- Venmo: direction phrases ("You paid", "paid you", etc.) present
- Venmo: per-transaction emails actually arriving (if zero in 60 days → user must enable in Venmo Me → Settings → Notifications → Email)

If any assumption fails, update `parsing-knowledge.md` with the actual pattern AND update the corresponding parser task below before writing test code.

- [ ] **Step 4: Commit the inspection output (without the fixtures themselves — already gitignored)**

```powershell
git add receipt-inspection.md
git status
```

Note: `receipt-inspection.md` is currently in `.gitignore`. If the report is safe to commit (no PII in normalized output), temporarily remove its `.gitignore` entry, commit, and either restore the gitignore or leave it tracked. **If unsure, do not commit and keep it local.**

---

## Phase 1: Project scaffolding

### Task 1: Restructure repo and create Python project skeleton

The existing Chrome extension lives at the repo root. Move it to `chrome-extension/` to make room for the Python codebase, then create the Python project structure with `pyproject.toml`.

**Files:**
- Move: `manifest.json`, `popup/`, `content/`, `background/`, `icons/`, `test/`, `INSTALLATION.md`, `FILES_CREATED.md`, `create_icons.py` → `chrome-extension/`
- Modify: `README.md` (top-level rewrite — see Task 26)
- Create: `pyproject.toml`, `bot/__init__.py`, `bot/parsers/__init__.py`, `bot/reporters/__init__.py`, `tests/__init__.py`, `tests/fixtures/.gitkeep`, `.env.example`, `config.yaml.example`

- [ ] **Step 1: Move existing extension files into `chrome-extension/`**

```powershell
New-Item -ItemType Directory -Path "chrome-extension" -Force | Out-Null
$items = @('manifest.json','popup','content','background','icons','test','INSTALLATION.md','FILES_CREATED.md','create_icons.py')
foreach ($i in $items) { if (Test-Path $i) { Move-Item -Path $i -Destination "chrome-extension\$i" -Force } }
```

- [ ] **Step 2: Create Python project layout**

```powershell
New-Item -ItemType Directory -Path "bot\parsers","bot\reporters","tests\fixtures" -Force | Out-Null
New-Item -ItemType File -Path "bot\__init__.py","bot\parsers\__init__.py","bot\reporters\__init__.py","tests\__init__.py","tests\fixtures\.gitkeep" -Force | Out-Null
```

- [ ] **Step 3: Write `pyproject.toml`**

```toml
[project]
name = "ynab-helper"
version = "0.1.0"
description = "Telegram-based YNAB transaction categorizer with Gmail and local LLM"
requires-python = ">=3.12"
dependencies = [
    "python-telegram-bot>=21.0",
    "google-api-python-client>=2.120",
    "google-auth-oauthlib>=1.2",
    "ynab>=1.0",
    "httpx>=0.27",
    "beautifulsoup4>=4.12",
    "lxml>=5.0",
    "pydantic>=2.7",
    "pydantic-settings>=2.3",
    "python-dotenv>=1.0",
    "pyyaml>=6.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "pytest-mock>=3.12",
    "respx>=0.21",  # httpx mocking
]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["bot*"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 4: Write `.env.example`**

```
# Copy to .env and fill in. .env is gitignored.
YNAB_TOKEN=
TELEGRAM_BOT_TOKEN=
```

- [ ] **Step 5: Write `config.yaml.example`**

```yaml
# Copy to config.yaml. config.yaml is gitignored.
gmail_accounts:
  - email: sbharri2@gmail.com
    user_id: steven
    token_path: ~/.google_workspace_mcp/credentials/sbharri2@gmail.com.json
    chat_id: 0  # set by first_run_setup.py

email_sources:
  - name: amazon
    query: "from:auto-confirm@amazon.com newer_than:2d"
    parser: amazon
  - name: venmo
    query: "from:venmo@venmo.com newer_than:2d"
    parser: venmo

ynab:
  budget_id: ""  # set by first_run_setup.py
  poll_interval_minutes: 30

ollama:
  endpoint: http://localhost:11434
  model: qwen2.5:14b
  temperature: 0.3

telegram:
  quiet_hours: "22:00-07:00"
  daily_digest_time: "09:00"

paths:
  database: ./ynab_helper.db
  log_dir: ./logs
```

- [ ] **Step 6: Create venv, install dependencies**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

Expected: clean install, no errors. If python-telegram-bot pulls in a conflict, run `pip install --upgrade pip setuptools wheel` first.

- [ ] **Step 7: Commit**

```powershell
git add pyproject.toml .env.example config.yaml.example bot tests chrome-extension
git rm -r --cached manifest.json popup content background icons test INSTALLATION.md FILES_CREATED.md create_icons.py 2>$null
git commit -m "scaffold: Python project layout; move Chrome extension to chrome-extension/"
```

---

### Task 2: Config loader with typed validation

`bot/config.py` loads `config.yaml` + `.env` into a typed `pydantic-settings` model. All other modules import the loaded `Settings` singleton — never read yaml/env directly.

**Files:**
- Create: `bot/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py
from pathlib import Path
import yaml
from bot.config import load_settings

def test_load_settings_from_example(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({
        "gmail_accounts": [{
            "email": "test@gmail.com",
            "user_id": "test",
            "token_path": "/tmp/tok.json",
            "chat_id": 123,
        }],
        "email_sources": [
            {"name": "amazon", "query": "from:x", "parser": "amazon"},
        ],
        "ynab": {"budget_id": "abc-123", "poll_interval_minutes": 30},
        "ollama": {"endpoint": "http://localhost:11434", "model": "qwen2.5:14b", "temperature": 0.3},
        "telegram": {"quiet_hours": "22:00-07:00", "daily_digest_time": "09:00"},
        "paths": {"database": "./test.db", "log_dir": "./logs"},
    }))
    monkeypatch.setenv("YNAB_TOKEN", "ynab-secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tg-secret")

    s = load_settings(cfg)

    assert s.gmail_accounts[0].email == "test@gmail.com"
    assert s.gmail_accounts[0].chat_id == 123
    assert s.ynab.budget_id == "abc-123"
    assert s.ynab_token == "ynab-secret"
    assert s.telegram_bot_token == "tg-secret"
    assert s.ollama.model == "qwen2.5:14b"
```

- [ ] **Step 2: Run test, verify it fails**

```powershell
pytest tests/test_config.py -v
```

Expected: ImportError or ModuleNotFoundError on `bot.config`.

- [ ] **Step 3: Implement `bot/config.py`**

```python
"""Typed configuration loaded from config.yaml + environment."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


class GmailAccount(BaseModel):
    email: str
    user_id: str
    token_path: str
    chat_id: int


class EmailSource(BaseModel):
    name: str
    query: str
    parser: Literal["amazon", "venmo"]


class YnabConfig(BaseModel):
    budget_id: str
    poll_interval_minutes: int = 30


class OllamaConfig(BaseModel):
    endpoint: str = "http://localhost:11434"
    model: str = "qwen2.5:14b"
    temperature: float = 0.3


class TelegramConfig(BaseModel):
    quiet_hours: str = "22:00-07:00"
    daily_digest_time: str = "09:00"


class Paths(BaseModel):
    database: str = "./ynab_helper.db"
    log_dir: str = "./logs"


class Settings(BaseModel):
    gmail_accounts: list[GmailAccount]
    email_sources: list[EmailSource]
    ynab: YnabConfig
    ollama: OllamaConfig
    telegram: TelegramConfig
    paths: Paths
    ynab_token: str = Field(default="", repr=False)
    telegram_bot_token: str = Field(default="", repr=False)


def load_settings(config_path: Path | str = "config.yaml") -> Settings:
    load_dotenv()
    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found at {config_path}. "
            f"Copy config.yaml.example to config.yaml and edit."
        )
    data = yaml.safe_load(config_path.read_text())
    data["ynab_token"] = os.getenv("YNAB_TOKEN", "")
    data["telegram_bot_token"] = os.getenv("TELEGRAM_BOT_TOKEN", "")
    return Settings.model_validate(data)
```

- [ ] **Step 4: Run test, verify it passes**

```powershell
pytest tests/test_config.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add bot/config.py tests/test_config.py
git commit -m "feat(config): typed Settings loader from config.yaml + env"
```

---

### Task 3: SQLite storage module with schema and CRUD helpers

`bot/storage.py` owns the database — schema creation, migrations, all CRUD. Other modules call helper functions; no module writes raw SQL outside this file.

**Files:**
- Create: `bot/storage.py`
- Test: `tests/test_storage.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_storage.py
import sqlite3
from pathlib import Path
from datetime import date

from bot.storage import (
    init_db, insert_pending_order, get_pending_orders, mark_order_categorized,
    insert_pending_txn, list_unmatched_amazon_orders, record_match,
)


def test_init_creates_all_tables(tmp_path):
    db = tmp_path / "test.db"
    init_db(db)
    con = sqlite3.connect(db)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    expected = {"pending_order", "pending_txn", "matched_charge", "bot_conversation", "audit_log"}
    assert expected.issubset(tables)


def test_pending_order_roundtrip(tmp_path):
    db = tmp_path / "test.db"
    init_db(db)
    oid = insert_pending_order(
        db,
        user_id="steven", source="amazon", external_id="123-4567890-1234567",
        email_id="msg-1", order_date=date(2026, 5, 12), total_cents=4723,
        raw_summary="Diapers, Wipes, Formula", raw_payload={"items": ["Diapers"]},
    )
    rows = get_pending_orders(db, status="pending")
    assert len(rows) == 1
    assert rows[0]["total_cents"] == 4723
    assert rows[0]["id"] == oid


def test_idempotent_insert_by_email_id(tmp_path):
    db = tmp_path / "test.db"
    init_db(db)
    args = dict(
        user_id="steven", source="amazon", external_id="A",
        email_id="dup", order_date=date(2026, 5, 12), total_cents=100,
        raw_summary="x", raw_payload={},
    )
    a = insert_pending_order(db, **args)
    b = insert_pending_order(db, **args)
    assert a == b  # returns existing id on duplicate email_id


def test_mark_categorized_and_match(tmp_path):
    db = tmp_path / "test.db"
    init_db(db)
    oid = insert_pending_order(
        db, user_id="steven", source="amazon", external_id="A",
        email_id="m1", order_date=date(2026,5,12), total_cents=4723,
        raw_summary="x", raw_payload={},
    )
    mark_order_categorized(db, oid, chosen_category="Baby Supplies")
    rows = get_pending_orders(db, status="categorized")
    assert rows[0]["chosen_category"] == "Baby Supplies"

    unmatched = list_unmatched_amazon_orders(db)
    assert len(unmatched) == 1

    record_match(db, pending_order_id=oid, ynab_txn_id="ynab-tx-1")
    unmatched2 = list_unmatched_amazon_orders(db)
    assert len(unmatched2) == 0
```

- [ ] **Step 2: Run test, verify it fails**

```powershell
pytest tests/test_storage.py -v
```

Expected: ImportError on `bot.storage`.

- [ ] **Step 3: Implement `bot/storage.py`**

```python
"""SQLite storage layer for ynab-helper.

All schema and CRUD lives here. No other module reads/writes the DB directly.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

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
    """Idempotent on email_id — returns existing id if already inserted."""
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
             total_cents, raw_summary, json.dumps(raw_payload)),
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
        con.execute(
            """
            UPDATE pending_order
            SET chosen_category = ?, chosen_at = ?, status = 'categorized',
                updated_at = ?
            WHERE id = ?
            """,
            (chosen_category, datetime.utcnow(), datetime.utcnow(), order_id),
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
            (datetime.utcnow(), pending_order_id),
        )


def audit(db_path: Path | str, event: str, details: dict[str, Any] | str = "") -> None:
    payload = json.dumps(details) if isinstance(details, dict) else str(details)
    with connect(db_path) as con:
        con.execute("INSERT INTO audit_log (event, details) VALUES (?, ?)",
                    (event, payload))
```

- [ ] **Step 4: Run test, verify it passes**

```powershell
pytest tests/test_storage.py -v
```

Expected: 4 PASS.

- [ ] **Step 5: Commit**

```powershell
git add bot/storage.py tests/test_storage.py
git commit -m "feat(storage): SQLite schema + CRUD helpers for orders, txns, matches"
```

---

## Phase 2: Pure email parsers (TDD with fixtures)

These tests use real HTML fixtures saved by `scripts/inspect_receipts.py` in Task 0. If a fixture file referenced below doesn't exist for any reason, copy a representative real email from Gmail to that path before writing the test.

### Task 4: Amazon parser — order ID, total, date

Build out one parser function at a time, smallest first. Each step is a TDD cycle.

**Files:**
- Create: `bot/parsers/amazon.py`
- Test: `tests/test_amazon_parser.py`

- [ ] **Step 1: Write the failing test for `extract_order_id`**

```python
# tests/test_amazon_parser.py
from pathlib import Path
from bot.parsers import amazon

FIXTURES = Path(__file__).parent / "fixtures" / "amazon_emails"


def _load(name: str) -> str:
    """Load first matching fixture by filename prefix."""
    matches = list(FIXTURES.glob(f"{name}*.html"))
    assert matches, f"No fixture found matching {name}*.html"
    return matches[0].read_text(encoding="utf-8")


def test_extract_order_id_from_confirmation():
    html = _load("auto-confirm-amazon-com")
    order_id = amazon.extract_order_id(html)
    # Format: 123-4567890-1234567
    assert order_id is not None
    parts = order_id.split("-")
    assert len(parts) == 3
    assert len(parts[0]) == 3
    assert len(parts[1]) == 7
    assert len(parts[2]) == 7
```

- [ ] **Step 2: Run test, verify it fails**

```powershell
pytest tests/test_amazon_parser.py::test_extract_order_id_from_confirmation -v
```

Expected: ImportError on `bot.parsers.amazon`.

- [ ] **Step 3: Implement minimal `extract_order_id`**

```python
# bot/parsers/amazon.py
"""Parse Amazon order-confirmation emails into structured order dicts.

All functions are pure — string in, dict/value out. No I/O.
"""
from __future__ import annotations

import re

ORDER_ID_RE = re.compile(r"\d{3}-\d{7}-\d{7}")


def extract_order_id(html: str) -> str | None:
    m = ORDER_ID_RE.search(html)
    return m.group(0) if m else None
```

- [ ] **Step 4: Run test, verify it passes**

```powershell
pytest tests/test_amazon_parser.py::test_extract_order_id_from_confirmation -v
```

Expected: PASS.

- [ ] **Step 5: Write the failing test for `extract_total_cents`**

```python
def test_extract_total_cents():
    html = _load("auto-confirm-amazon-com")
    total = amazon.extract_total_cents(html)
    assert total is not None
    assert total > 0
    assert total < 100_000_00  # under $100k sanity
```

- [ ] **Step 6: Run test, verify it fails**

```powershell
pytest tests/test_amazon_parser.py::test_extract_total_cents -v
```

Expected: AttributeError — `extract_total_cents` not defined.

- [ ] **Step 7: Implement `extract_total_cents`**

```python
# Add to bot/parsers/amazon.py
TOTAL_RE = re.compile(r"Order Total[:\s]*\$?([\d,]+\.\d{2})", re.I)
TOTAL_FALLBACK_RE = re.compile(r"\bTotal[:\s]*\$?([\d,]+\.\d{2})", re.I)


def extract_total_cents(html: str) -> int | None:
    """Prefer 'Order Total: $X.XX'; fall back to first 'Total: $X.XX'."""
    for regex in (TOTAL_RE, TOTAL_FALLBACK_RE):
        m = regex.search(html)
        if m:
            dollars_str = m.group(1).replace(",", "")
            return int(round(float(dollars_str) * 100))
    return None
```

- [ ] **Step 8: Run test, verify it passes**

```powershell
pytest tests/test_amazon_parser.py::test_extract_total_cents -v
```

Expected: PASS.

- [ ] **Step 9: Write the failing test for `extract_order_date`**

```python
from datetime import date

def test_extract_order_date():
    html = _load("auto-confirm-amazon-com")
    d = amazon.extract_order_date(html)
    assert isinstance(d, date)
    # Sanity: within the last 60 days (fixture is recent)
    from datetime import timedelta
    assert (date.today() - d) < timedelta(days=120)
```

- [ ] **Step 10: Run test, verify it fails**

```powershell
pytest tests/test_amazon_parser.py::test_extract_order_date -v
```

Expected: AttributeError.

- [ ] **Step 11: Implement `extract_order_date`**

```python
# Add to bot/parsers/amazon.py
from datetime import date, datetime

DATE_RE_FULL = re.compile(r"Order placed[:\s]+([A-Z][a-z]+ \d{1,2},? \d{4})", re.I)
DATE_RE_SHORT = re.compile(r"Order placed[:\s]+([A-Z][a-z]+ \d{1,2})", re.I)
DATE_RE_ARRIVING = re.compile(r"Arriving[:\s]+([A-Z][a-z]+ \d{1,2})", re.I)


def _parse_amazon_date(text: str, today: date | None = None) -> date | None:
    today = today or date.today()
    text = text.replace(",", "").strip()
    for fmt in ("%B %d %Y", "%B %d"):
        try:
            d = datetime.strptime(text, fmt).date()
            if fmt == "%B %d":
                d = d.replace(year=today.year)
                # If parsed month is in the future relative to today, assume last year
                if d > today:
                    d = d.replace(year=today.year - 1)
            return d
        except ValueError:
            continue
    return None


def extract_order_date(html: str, today: date | None = None) -> date | None:
    for regex in (DATE_RE_FULL, DATE_RE_SHORT):
        m = regex.search(html)
        if m:
            d = _parse_amazon_date(m.group(1), today=today)
            if d:
                return d
    # Last resort: "Arriving" date is a delivery estimate — not ideal but better than nothing
    m = DATE_RE_ARRIVING.search(html)
    if m:
        return _parse_amazon_date(m.group(1), today=today)
    return None
```

- [ ] **Step 12: Run test, verify it passes**

```powershell
pytest tests/test_amazon_parser.py::test_extract_order_date -v
```

Expected: PASS.

- [ ] **Step 13: Commit**

```powershell
git add bot/parsers/amazon.py tests/test_amazon_parser.py
git commit -m "feat(amazon-parser): extract order_id, total_cents, order_date"
```

---

### Task 5: Amazon parser — items and top-level `parse()`

**Files:**
- Modify: `bot/parsers/amazon.py`
- Modify: `tests/test_amazon_parser.py`

- [ ] **Step 1: Write the failing test for `extract_items`**

```python
def test_extract_items_returns_nonempty_list():
    html = _load("auto-confirm-amazon-com")
    items = amazon.extract_items(html)
    assert isinstance(items, list)
    assert len(items) > 0
    for item in items:
        assert 5 <= len(item) <= 200  # validation bounds from existing scraper
        assert isinstance(item, str)
```

- [ ] **Step 2: Run test, verify it fails**

Expected: AttributeError on `extract_items`.

- [ ] **Step 3: Implement `extract_items`**

```python
# Add to bot/parsers/amazon.py
from bs4 import BeautifulSoup


def extract_items(html: str) -> list[str]:
    """Items are titles linked from /dp/ or /gp/product/ hrefs; dedupe; 5-200 char range."""
    soup = BeautifulSoup(html, "lxml")
    seen: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/dp/" not in href and "/gp/product/" not in href:
            continue
        text = a.get_text(strip=True)
        if not (5 <= len(text) <= 200):
            continue
        if text in seen:
            continue
        seen.append(text)
    if seen:
        return seen
    # Fallback: image alt text on product images
    for img in soup.find_all("img", alt=True):
        alt = img["alt"].strip()
        if 5 <= len(alt) <= 200 and alt not in seen:
            seen.append(alt)
    return seen or ["(could not parse items)"]
```

- [ ] **Step 4: Run test, verify it passes**

```powershell
pytest tests/test_amazon_parser.py::test_extract_items_returns_nonempty_list -v
```

Expected: PASS.

- [ ] **Step 5: Write the failing test for top-level `parse()`**

```python
def test_parse_returns_complete_dict():
    html = _load("auto-confirm-amazon-com")
    result = amazon.parse(html)

    assert result["parse_status"] in {"ok", "partial"}
    assert result["order_id"]
    assert result["total_cents"] > 0
    assert isinstance(result["order_date"], date)
    assert len(result["items"]) > 0
    assert result["source"] == "amazon"


def test_parse_handles_garbage_input():
    result = amazon.parse("<html>not a real amazon email</html>")
    assert result["parse_status"] == "partial"
    assert result["order_id"] is None
    assert result["items"] == ["(could not parse items)"]
```

- [ ] **Step 6: Run test, verify both fail**

Expected: AttributeError on `parse`.

- [ ] **Step 7: Implement `parse()`**

```python
# Add to bot/parsers/amazon.py
def parse(html: str, *, today: date | None = None) -> dict:
    """Top-level parser. Never raises — returns partial dict on failure."""
    order_id = extract_order_id(html)
    total_cents = extract_total_cents(html)
    order_date = extract_order_date(html, today=today)
    items = extract_items(html)

    missing = [
        name for name, val in
        [("order_id", order_id), ("total_cents", total_cents), ("order_date", order_date)]
        if val is None
    ]
    return {
        "source": "amazon",
        "order_id": order_id,
        "total_cents": total_cents,
        "order_date": order_date,
        "items": items,
        "summary": f"{len(items)} item(s): " + ", ".join(items[:3])
                    + (f" (+{len(items)-3} more)" if len(items) > 3 else ""),
        "parse_status": "ok" if not missing else "partial",
        "missing_fields": missing,
    }
```

- [ ] **Step 8: Run tests, verify both pass**

```powershell
pytest tests/test_amazon_parser.py -v
```

Expected: ALL PASS.

- [ ] **Step 9: Commit**

```powershell
git add bot/parsers/amazon.py tests/test_amazon_parser.py
git commit -m "feat(amazon-parser): extract_items + top-level parse() with parse_status"
```

---

### Task 6: Venmo parser

Mirror Amazon's pattern but for Venmo's direction + counterparty + note + amount.

**Files:**
- Create: `bot/parsers/venmo.py`
- Test: `tests/test_venmo_parser.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_venmo_parser.py
from pathlib import Path
from datetime import date
from bot.parsers import venmo

FIXTURES = Path(__file__).parent / "fixtures" / "venmo_emails"


def _load(name: str) -> tuple[str, str]:
    """Returns (html, text) for first fixture matching name prefix."""
    html_matches = list(FIXTURES.glob(f"{name}*.html"))
    txt_matches = list(FIXTURES.glob(f"{name}*.txt"))
    assert html_matches or txt_matches, f"No fixture for {name}*"
    html = html_matches[0].read_text(encoding="utf-8") if html_matches else ""
    text = txt_matches[0].read_text(encoding="utf-8") if txt_matches else ""
    return html, text


def test_extract_direction():
    html, text = _load("venmo")
    body = text or html
    direction = venmo.extract_direction(body)
    assert direction in {"paid", "received", "charged", "charged_by"}


def test_extract_amount_cents():
    html, text = _load("venmo")
    body = text or html
    amt = venmo.extract_amount_cents(body)
    assert amt is not None and amt > 0


def test_parse_returns_complete_dict():
    html, text = _load("venmo")
    result = venmo.parse(html or text)
    assert result["source"] == "venmo"
    assert result["parse_status"] in {"ok", "partial"}
    assert result["amount_cents"] > 0
    assert result["direction"] in {"paid", "received", "charged", "charged_by", None}
```

- [ ] **Step 2: Run tests, verify they fail**

```powershell
pytest tests/test_venmo_parser.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement `bot/parsers/venmo.py`**

```python
"""Parse Venmo per-transaction emails into structured txn dicts.

All functions are pure — string in, dict/value out. No I/O.

The user must enable per-transaction email notifications in the Venmo app:
  Me → Settings → Notifications → Email → Payments sent + Payments received
"""
from __future__ import annotations

import re
from datetime import date
from bs4 import BeautifulSoup

DIRECTION_PATTERNS = [
    (re.compile(r"You paid", re.I), "paid"),
    (re.compile(r"You charged", re.I), "charged"),
    (re.compile(r"paid you", re.I), "received"),
    (re.compile(r"charged you", re.I), "charged_by"),
]

AMOUNT_RE = re.compile(r"\$\s*([\d,]+\.\d{2})")
# Counterparty: best-effort — Venmo headlines tend to look like
#   "Sarah Chen paid you $42.00"  or  "You paid Sarah Chen $42.00"
COUNTERPARTY_PATTERNS = [
    re.compile(r"^([A-Z][\w'\-\. ]{1,50}) paid you\b", re.M),
    re.compile(r"^([A-Z][\w'\-\. ]{1,50}) charged you\b", re.M),
    re.compile(r"You paid ([A-Z][\w'\-\. ]{1,50})\b", re.I),
    re.compile(r"You charged ([A-Z][\w'\-\. ]{1,50})\b", re.I),
]


def _html_to_text(html: str) -> str:
    if not html:
        return ""
    return BeautifulSoup(html, "lxml").get_text("\n", strip=True)


def extract_direction(body: str) -> str | None:
    for regex, label in DIRECTION_PATTERNS:
        if regex.search(body):
            return label
    return None


def extract_amount_cents(body: str) -> int | None:
    m = AMOUNT_RE.search(body)
    if not m:
        return None
    return int(round(float(m.group(1).replace(",", "")) * 100))


def extract_counterparty(body: str) -> str | None:
    for regex in COUNTERPARTY_PATTERNS:
        m = regex.search(body)
        if m:
            return m.group(1).strip()
    return None


def extract_note(body: str) -> str:
    """The note is typically the line immediately after the headline.
    Best-effort: grab a quoted substring or the first short line after the amount."""
    # Quoted-string heuristic
    qm = re.search(r'[""\'"]([^""\'"]{2,200})[""\'"]', body)
    if qm:
        return qm.group(1).strip()
    return ""


def parse(html_or_text: str, *, today: date | None = None) -> dict:
    """Top-level parser. Never raises."""
    text = _html_to_text(html_or_text) if "<" in html_or_text else html_or_text
    direction = extract_direction(text)
    amount = extract_amount_cents(text)
    counterparty = extract_counterparty(text)
    note = extract_note(text)
    order_date = today or date.today()

    missing = [n for n, v in [("direction", direction), ("amount_cents", amount)] if v is None]
    return {
        "source": "venmo",
        "direction": direction,
        "counterparty": counterparty,
        "note": note,
        "amount_cents": amount or 0,
        "order_date": order_date,
        "summary": f"{direction or '?'} {counterparty or '?'} ${(amount or 0)/100:.2f}"
                   + (f' — "{note}"' if note else ""),
        "parse_status": "ok" if not missing else "partial",
        "missing_fields": missing,
    }
```

- [ ] **Step 4: Run tests, verify they pass**

```powershell
pytest tests/test_venmo_parser.py -v
```

Expected: PASS. If `test_extract_direction` fails because no fixture exists, confirm in Task 0 that Venmo notifications were enabled and re-run inspection.

- [ ] **Step 5: Commit**

```powershell
git add bot/parsers/venmo.py tests/test_venmo_parser.py
git commit -m "feat(venmo-parser): direction, amount, counterparty, note extraction + parse()"
```

---

## Phase 3: External clients

### Task 7: YNAB API client wrapper

Thin wrapper around the official `ynab` SDK. Centralizes auth, error handling, and the specific calls we use.

**Files:**
- Create: `bot/ynab_client.py`
- Test: `tests/test_ynab_client.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ynab_client.py
from unittest.mock import MagicMock, patch
from datetime import date
from bot.ynab_client import YnabClient


@patch("bot.ynab_client.ynab")
def test_list_uncategorized_calls_api(mock_ynab):
    mock_api = MagicMock()
    mock_api.get_transactions.return_value.data.transactions = [
        MagicMock(
            id="tx-1", account_id="acc-1", payee_name="STARBUCKS",
            amount=-12750, date=date(2026, 5, 14), memo="",
            category_id=None,
        )
    ]
    mock_ynab.ApiClient.return_value.__enter__.return_value = MagicMock()
    mock_ynab.TransactionsApi.return_value = mock_api

    client = YnabClient(token="x", budget_id="b1")
    txns = client.list_uncategorized()

    assert len(txns) == 1
    assert txns[0]["ynab_txn_id"] == "tx-1"
    assert txns[0]["amount_cents"] == -1275  # YNAB milliunits → cents (÷10)


@patch("bot.ynab_client.ynab")
def test_set_category(mock_ynab):
    mock_api = MagicMock()
    mock_ynab.ApiClient.return_value.__enter__.return_value = MagicMock()
    mock_ynab.TransactionsApi.return_value = mock_api

    client = YnabClient(token="x", budget_id="b1")
    client.set_category("tx-1", "cat-uuid")

    mock_api.update_transaction.assert_called_once()
```

- [ ] **Step 2: Run test, verify it fails**

```powershell
pytest tests/test_ynab_client.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement `bot/ynab_client.py`**

```python
"""Thin wrapper around the official YNAB Python SDK.

Centralizes auth + the specific operations ynab-helper uses. All amounts
in milliunits are converted to cents (×0.1) for internal use; on write
we convert back (×10). YNAB stores outflows as negative.
"""
from __future__ import annotations

import logging
from datetime import date

import ynab

log = logging.getLogger(__name__)


def _milliunits_to_cents(m: int) -> int:
    return m // 10


def _cents_to_milliunits(c: int) -> int:
    return c * 10


class YnabClient:
    def __init__(self, token: str, budget_id: str):
        self.budget_id = budget_id
        self._config = ynab.Configuration(access_token=token)

    def _api(self):
        return ynab.ApiClient(self._config)

    def list_uncategorized(self, since: date | None = None) -> list[dict]:
        """Returns all uncategorized transactions, optionally since a date."""
        with self._api() as api_client:
            api = ynab.TransactionsApi(api_client)
            kwargs = {"budget_id": self.budget_id}
            if since:
                kwargs["since_date"] = since
            resp = api.get_transactions(**kwargs)
            results = []
            for t in resp.data.transactions:
                if t.category_id is not None:
                    continue
                results.append({
                    "ynab_txn_id": t.id,
                    "ynab_account_id": t.account_id,
                    "payee": t.payee_name or "",
                    "amount_cents": _milliunits_to_cents(t.amount),
                    "txn_date": t.date,
                    "memo": t.memo or "",
                })
            return results

    def set_category(self, ynab_txn_id: str, category_id: str) -> None:
        with self._api() as api_client:
            api = ynab.TransactionsApi(api_client)
            api.update_transaction(
                budget_id=self.budget_id,
                transaction_id=ynab_txn_id,
                data=ynab.PutTransactionWrapper(
                    transaction=ynab.ExistingTransaction(category_id=category_id)
                ),
            )

    def list_categories(self) -> list[dict]:
        """Returns flat list of categories with parent group name attached."""
        with self._api() as api_client:
            api = ynab.CategoriesApi(api_client)
            resp = api.get_categories(budget_id=self.budget_id)
            results = []
            for group in resp.data.category_groups:
                if group.hidden or group.deleted:
                    continue
                for c in group.categories:
                    if c.hidden or c.deleted:
                        continue
                    results.append({
                        "id": c.id,
                        "name": c.name,
                        "group": group.name,
                    })
            return results
```

- [ ] **Step 4: Run tests, verify they pass**

```powershell
pytest tests/test_ynab_client.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add bot/ynab_client.py tests/test_ynab_client.py
git commit -m "feat(ynab): client wrapper with list_uncategorized + set_category + list_categories"
```

---

### Task 8: Ollama categorizer

Sends a structured prompt to local Ollama, returns `{category_id, confidence, reasoning}`. Uses JSON-mode output for parsing reliability.

**Files:**
- Create: `bot/categorizer.py`
- Test: `tests/test_categorizer.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_categorizer.py
import json
from unittest.mock import patch, MagicMock
from bot.categorizer import Categorizer


@patch("bot.categorizer.httpx.Client")
def test_suggests_category_from_items(mock_client_cls):
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.post.return_value.json.return_value = {
        "message": {"content": json.dumps({
            "category_id": "cat-baby-supplies",
            "confidence": 0.92,
            "reasoning": "Items are clearly baby-related."
        })}
    }
    mock_client_cls.return_value = mock_client

    categories = [
        {"id": "cat-baby-supplies", "name": "Baby Supplies", "group": "Family"},
        {"id": "cat-groceries", "name": "Groceries", "group": "Food"},
    ]
    cat = Categorizer(endpoint="http://localhost:11434", model="qwen2.5:14b")
    result = cat.suggest(
        summary="3 items: Diapers Size 4, Wipes 800ct, Formula",
        amount_cents=4723,
        date_str="2026-05-12",
        source="amazon",
        categories=categories,
    )
    assert result["category_id"] == "cat-baby-supplies"
    assert result["confidence"] > 0.5


@patch("bot.categorizer.httpx.Client")
def test_handles_ollama_down_gracefully(mock_client_cls):
    mock_client_cls.side_effect = Exception("connection refused")

    cat = Categorizer(endpoint="http://localhost:11434", model="qwen2.5:14b")
    result = cat.suggest(
        summary="x", amount_cents=100, date_str="2026-05-12",
        source="amazon", categories=[],
    )
    assert result["category_id"] is None
    assert result["confidence"] == 0.0
    assert "error" in result
```

- [ ] **Step 2: Run test, verify it fails**

```powershell
pytest tests/test_categorizer.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement `bot/categorizer.py`**

```python
"""Local Ollama categorizer.

Talks to Ollama's /api/chat endpoint using format='json' to constrain output
to parseable JSON. Returns {category_id, confidence, reasoning, error?}.
On any failure, returns category_id=None so the bot falls back to showing
plain category buttons without a suggestion.
"""
from __future__ import annotations

import json
import logging

import httpx

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a personal finance assistant categorizing a transaction for a YNAB budget.
Given a transaction (with its source, description, amount, and date) and a list
of available YNAB categories, choose the single best-fit category.

Respond ONLY with JSON matching this exact shape:
{
  "category_id": "<id of the chosen category, or null if none fits>",
  "confidence": <float 0.0-1.0>,
  "reasoning": "<one sentence>"
}
"""


class Categorizer:
    def __init__(self, endpoint: str, model: str, temperature: float = 0.3):
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.temperature = temperature

    def suggest(
        self,
        *,
        summary: str,
        amount_cents: int,
        date_str: str,
        source: str,
        categories: list[dict],
    ) -> dict:
        user_msg = self._build_user_message(
            summary=summary, amount_cents=amount_cents,
            date_str=date_str, source=source, categories=categories,
        )
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(
                    f"{self.endpoint}/api/chat",
                    json={
                        "model": self.model,
                        "format": "json",
                        "stream": False,
                        "options": {"temperature": self.temperature},
                        "messages": [
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": user_msg},
                        ],
                    },
                )
            data = resp.json()
            content = data.get("message", {}).get("content", "{}")
            parsed = json.loads(content)
            return {
                "category_id": parsed.get("category_id"),
                "confidence": float(parsed.get("confidence", 0.0)),
                "reasoning": parsed.get("reasoning", ""),
            }
        except Exception as e:
            log.warning("ollama suggest failed: %s", e)
            return {"category_id": None, "confidence": 0.0, "reasoning": "", "error": str(e)}

    @staticmethod
    def _build_user_message(*, summary, amount_cents, date_str, source, categories):
        cat_lines = "\n".join(
            f"- {c['id']}: {c['group']} > {c['name']}" for c in categories
        )
        return (
            f"Transaction source: {source}\n"
            f"Date: {date_str}\n"
            f"Amount: ${amount_cents/100:.2f}\n"
            f"Description: {summary}\n\n"
            f"Available categories (id: group > name):\n{cat_lines}\n\n"
            "Choose the best-fit category and respond as JSON only."
        )
```

- [ ] **Step 4: Run tests, verify they pass**

```powershell
pytest tests/test_categorizer.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add bot/categorizer.py tests/test_categorizer.py
git commit -m "feat(categorizer): Ollama-backed category suggester with graceful fallback"
```

---

### Task 9: Matcher (pure scoring function)

**Files:**
- Create: `bot/matcher.py`
- Test: `tests/test_matcher.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_matcher.py
from datetime import date, timedelta
from bot.matcher import match_score, find_best_match


def _order(total, days_ago=2):
    return {
        "id": 1,
        "total_cents": total,
        "order_date": date.today() - timedelta(days=days_ago),
        "external_id": "123-4567890-1234567",
    }


def _txn(amount, days_ago=0, payee="AMAZON.COM*ABC123", memo=""):
    return {
        "ynab_txn_id": "tx-1",
        "amount_cents": -amount,  # YNAB outflows are negative
        "txn_date": date.today() - timedelta(days=days_ago),
        "payee": payee,
        "memo": memo,
    }


def test_exact_amount_same_day_high_score():
    s = match_score(_order(4723, days_ago=0), _txn(4723, days_ago=0))
    assert s >= 0.85


def test_drift_amount_lower_score():
    s = match_score(_order(4723, days_ago=2), _txn(4500, days_ago=4))
    assert 0.5 <= s < 0.85


def test_too_old_zero_score():
    s = match_score(_order(4723, days_ago=20), _txn(4723, days_ago=0))
    assert s < 0.3


def test_memo_bonus_when_order_id_present():
    s_no = match_score(_order(4723), _txn(4723))
    s_with = match_score(_order(4723), _txn(4723, memo="AMAZON 123-4567890-1234567"))
    assert s_with > s_no


def test_find_best_match_returns_none_when_no_candidates_above_threshold():
    pending = [_order(4723, days_ago=2)]
    txn = _txn(99999, days_ago=2)  # nothing close
    result = find_best_match(pending, txn, threshold=0.85)
    assert result is None


def test_find_best_match_returns_unambiguous_winner():
    pending = [_order(4723, days_ago=2), _order(9999, days_ago=2)]
    txn = _txn(4723, days_ago=2)
    result = find_best_match(pending, txn, threshold=0.85)
    assert result is not None
    assert result["id"] == 1


def test_find_best_match_returns_none_when_ambiguous():
    """Two candidates within 0.10 of each other → ambiguous → no auto-match."""
    pending = [_order(4723, days_ago=2), _order(4723, days_ago=3)]
    txn = _txn(4723, days_ago=2)
    result = find_best_match(pending, txn, threshold=0.85, ambiguity_gap=0.10)
    assert result is None
```

- [ ] **Step 2: Run tests, verify they fail**

```powershell
pytest tests/test_matcher.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement `bot/matcher.py`**

```python
"""Pure scoring function: how well does a YNAB transaction match a pending order?

Confidence score in [0.0, 1.10]. The memo_bonus term (worth +0.10) pushes
scores above 1.0 when the YNAB memo contains the Amazon order id.
"""
from __future__ import annotations

import re
from datetime import date

ORDER_ID_RE = re.compile(r"\d{3}-\d{7}-\d{7}")
PAYEE_AMAZON_RE = re.compile(r"\bAMAZON", re.I)
PAYEE_VENMO_RE = re.compile(r"\bVENMO", re.I)


def _amount_score(order_cents: int, txn_cents: int) -> float:
    """1.0 if exact; linear falloff to 0 at ±10% (capped at $10)."""
    o = abs(order_cents)
    t = abs(txn_cents)
    if o == t:
        return 1.0
    drift = abs(o - t)
    tolerance = min(o // 10, 1000)  # 10% or $10, whichever is smaller
    if tolerance == 0 or drift > tolerance:
        return 0.0
    return max(0.0, 1.0 - (drift / tolerance))


def _date_score(order_date: date, txn_date: date) -> float:
    """1.0 if same day; linear falloff to 0 at 14 days. Negative if txn before order."""
    delta = (txn_date - order_date).days
    if delta < 0 or delta > 14:
        return 0.0
    return 1.0 - (delta / 14.0)


def _payee_score(payee: str, source: str) -> float:
    regex = PAYEE_AMAZON_RE if source == "amazon" else PAYEE_VENMO_RE
    return 1.0 if regex.search(payee or "") else 0.0


def _memo_bonus(memo: str, order_id: str | None) -> float:
    if not memo or not order_id:
        return 0.0
    return 1.0 if order_id in memo else 0.0


def match_score(pending_order: dict, ynab_txn: dict, *, source: str = "amazon") -> float:
    return (
        _amount_score(pending_order["total_cents"], ynab_txn["amount_cents"]) * 0.50
        + _date_score(pending_order["order_date"], ynab_txn["txn_date"]) * 0.30
        + _payee_score(ynab_txn.get("payee", ""), source) * 0.20
        + _memo_bonus(ynab_txn.get("memo", ""), pending_order.get("external_id")) * 0.10
    )


def find_best_match(
    pending_orders: list[dict],
    ynab_txn: dict,
    *,
    threshold: float = 0.85,
    ambiguity_gap: float = 0.10,
    source: str = "amazon",
) -> dict | None:
    """Return the single highest-scoring order if score >= threshold AND no other
    candidate is within ambiguity_gap of it. Otherwise return None (caller asks user)."""
    if not pending_orders:
        return None
    scored = [(match_score(o, ynab_txn, source=source), o) for o in pending_orders]
    scored.sort(key=lambda x: -x[0])
    top_score, top_order = scored[0]
    if top_score < threshold:
        return None
    if len(scored) > 1:
        runner_up_score = scored[1][0]
        if (top_score - runner_up_score) < ambiguity_gap:
            return None  # ambiguous
    return top_order
```

- [ ] **Step 4: Run tests, verify they pass**

```powershell
pytest tests/test_matcher.py -v
```

Expected: 7 PASS.

- [ ] **Step 5: Commit**

```powershell
git add bot/matcher.py tests/test_matcher.py
git commit -m "feat(matcher): scoring function + find_best_match with ambiguity guard"
```

---

## Phase 4: Watchers (orchestration)

### Task 10: Gmail watcher

Multi-source email poller. For each configured source, runs the Gmail query, fetches new messages, calls the corresponding parser, inserts to `pending_order`, runs the categorizer, and notifies the Telegram bot via a job-queue table (`pending_order.status = 'pending'` is the queue).

**Files:**
- Create: `bot/gmail_watcher.py`
- Test: `tests/test_gmail_watcher.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_gmail_watcher.py
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import date

from bot.config import Settings, GmailAccount, EmailSource, YnabConfig, OllamaConfig, TelegramConfig, Paths
from bot.gmail_watcher import poll_once
from bot import storage


def _settings(tmp_path):
    return Settings(
        gmail_accounts=[GmailAccount(
            email="t@gmail.com", user_id="steven",
            token_path=str(tmp_path / "tok.json"), chat_id=1,
        )],
        email_sources=[EmailSource(name="amazon", query="from:auto-confirm@amazon.com", parser="amazon")],
        ynab=YnabConfig(budget_id="b"),
        ollama=OllamaConfig(),
        telegram=TelegramConfig(),
        paths=Paths(database=str(tmp_path / "test.db")),
    )


@patch("bot.gmail_watcher._build_gmail_service")
@patch("bot.gmail_watcher.Categorizer")
@patch("bot.gmail_watcher.YnabClient")
def test_poll_once_inserts_pending_order(mock_ynab_cls, mock_cat_cls, mock_build, tmp_path):
    db = tmp_path / "test.db"
    storage.init_db(db)

    # Mock Gmail
    svc = MagicMock()
    svc.users().messages().list().execute.return_value = {"messages": [{"id": "m-1"}]}
    svc.users().messages().get().execute.return_value = {
        "id": "m-1",
        "payload": {"parts": [{"mimeType": "text/html",
                                "body": {"data": _b64(_sample_amazon_html())}}],
                    "headers": []},
    }
    mock_build.return_value = svc

    # Mock categorizer to suggest something
    mock_cat = MagicMock()
    mock_cat.suggest.return_value = {"category_id": "c1", "confidence": 0.9, "reasoning": "x"}
    mock_cat_cls.return_value = mock_cat

    # Mock YNAB to return one category
    mock_ynab = MagicMock()
    mock_ynab.list_categories.return_value = [{"id": "c1", "name": "Baby", "group": "Family"}]
    mock_ynab_cls.return_value = mock_ynab

    settings = _settings(tmp_path)
    new_count = poll_once(settings)

    assert new_count == 1
    rows = storage.get_pending_orders(db, status="pending")
    assert len(rows) == 1
    assert rows[0]["suggested_category"] == "c1"


def _b64(s: str) -> str:
    import base64
    return base64.urlsafe_b64encode(s.encode()).decode()


def _sample_amazon_html() -> str:
    return """
    <html><body>
    Order placed: May 12, 2026
    Order # 123-4567890-1234567
    <a href="/dp/B0ABC">Pampers Diapers Size 4</a>
    Order Total: $47.23
    </body></html>
    """
```

- [ ] **Step 2: Run test, verify it fails**

Expected: ImportError on `bot.gmail_watcher`.

- [ ] **Step 3: Implement `bot/gmail_watcher.py`**

```python
"""Multi-source Gmail poller.

For each (gmail_account × email_source) pair:
  1. Search Gmail for matching new messages
  2. Parse each with the source-specific parser
  3. Insert into pending_order (idempotent on email_id)
  4. Ask the categorizer for a suggestion
  5. Persist suggestion on the row

The Telegram bot independently watches for new pending_order rows and pushes
them to the user — this module does not talk to Telegram directly.
"""
from __future__ import annotations

import base64
import importlib
import logging
from datetime import date

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import sqlite3

from bot import storage
from bot.config import Settings
from bot.categorizer import Categorizer
from bot.ynab_client import YnabClient

log = logging.getLogger(__name__)


def _build_gmail_service(token_path: str):
    creds = Credentials.from_authorized_user_file(token_path)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(token_path, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _extract_body(msg: dict) -> str:
    def walk(part):
        body = part.get("body", {})
        if "data" in body:
            yield part.get("mimeType", ""), base64.urlsafe_b64decode(
                body["data"]
            ).decode("utf-8", errors="replace")
        for sub in part.get("parts", []) or []:
            yield from walk(sub)

    bodies = list(walk(msg["payload"]))
    html = next((b for m, b in bodies if "html" in m), None)
    text = next((b for m, b in bodies if m == "text/plain"), None)
    return html or text or ""


def _load_parser(name: str):
    mod = importlib.import_module(f"bot.parsers.{name}")
    return mod.parse


def poll_once(settings: Settings) -> int:
    """Run one polling pass across all (account × source) pairs. Returns new-row count."""
    storage.init_db(settings.paths.database)
    new_count = 0

    ynab = YnabClient(settings.ynab_token, settings.ynab.budget_id)
    categories = ynab.list_categories() if settings.ynab_token else []
    cat_engine = Categorizer(settings.ollama.endpoint, settings.ollama.model,
                              settings.ollama.temperature)

    for account in settings.gmail_accounts:
        svc = _build_gmail_service(account.token_path)
        for source in settings.email_sources:
            log.info("polling %s for %s", account.email, source.name)
            try:
                resp = svc.users().messages().list(
                    userId="me", q=source.query, maxResults=50
                ).execute()
            except Exception as e:
                log.error("gmail list failed: %s", e)
                continue
            for m in resp.get("messages", []):
                msg = svc.users().messages().get(userId="me", id=m["id"], format="full").execute()
                body = _extract_body(msg)
                parser = _load_parser(source.parser)
                parsed = parser(body)
                if parsed["parse_status"] not in {"ok", "partial"}:
                    continue
                try:
                    row_id = storage.insert_pending_order(
                        settings.paths.database,
                        user_id=account.user_id,
                        source=parsed["source"],
                        external_id=parsed.get("order_id") or parsed.get("counterparty"),
                        email_id=m["id"],
                        order_date=parsed.get("order_date") or date.today(),
                        total_cents=parsed.get("total_cents") or parsed.get("amount_cents") or 0,
                        raw_summary=parsed.get("summary", ""),
                        raw_payload=parsed,
                    )
                    new_count += 1
                except sqlite3.IntegrityError:
                    continue

                # Categorize
                suggestion = cat_engine.suggest(
                    summary=parsed.get("summary", ""),
                    amount_cents=parsed.get("total_cents") or parsed.get("amount_cents") or 0,
                    date_str=str(parsed.get("order_date") or ""),
                    source=parsed["source"],
                    categories=categories,
                )
                with storage.connect(settings.paths.database) as con:
                    con.execute(
                        "UPDATE pending_order SET suggested_category = ?, "
                        "suggested_confidence = ? WHERE id = ?",
                        (suggestion["category_id"], suggestion["confidence"], row_id),
                    )
                storage.audit(settings.paths.database, "pending_order_inserted",
                              {"id": row_id, "source": parsed["source"]})

    return new_count


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    from bot.config import load_settings
    settings = load_settings()
    n = poll_once(settings)
    log.info("poll_once: %d new pending_orders", n)
```

- [ ] **Step 4: Run test, verify it passes**

```powershell
pytest tests/test_gmail_watcher.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add bot/gmail_watcher.py tests/test_gmail_watcher.py
git commit -m "feat(gmail-watcher): poll Gmail per source, parse, insert, categorize"
```

---

### Task 11: YNAB watcher (poll + match + enqueue non-Amazon/Venmo)

Three jobs per run:
1. **Match:** for each newly-categorized pending_order without a matched_charge, look at recent YNAB AMAZON/VENMO charges and try to apply the category via the matcher.
2. **Enqueue:** for any YNAB transaction that's uncategorized AND not from Amazon/Venmo, insert into `pending_txn` (for the daily digest).
3. **Daily-digest trigger:** if the current time is within ±5 min of `daily_digest_time`, mark all pending_txns with a fresh `digest_run_id` and signal the bot.

**Files:**
- Create: `bot/ynab_watcher.py`
- Test: `tests/test_ynab_watcher.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_ynab_watcher.py
from datetime import date, datetime, time
from unittest.mock import patch, MagicMock
from bot import storage
from bot.config import Settings, GmailAccount, EmailSource, YnabConfig, OllamaConfig, TelegramConfig, Paths
from bot.ynab_watcher import poll_once


def _settings(tmp_path):
    return Settings(
        gmail_accounts=[GmailAccount(email="t@gmail.com", user_id="steven",
                                      token_path="x", chat_id=1)],
        email_sources=[EmailSource(name="amazon", query="x", parser="amazon")],
        ynab=YnabConfig(budget_id="b"),
        ollama=OllamaConfig(),
        telegram=TelegramConfig(),
        paths=Paths(database=str(tmp_path / "test.db")),
    )


@patch("bot.ynab_watcher.YnabClient")
def test_matches_categorized_order_to_amazon_charge(mock_cls, tmp_path):
    db = tmp_path / "test.db"
    storage.init_db(db)
    oid = storage.insert_pending_order(
        db, user_id="steven", source="amazon", external_id="123-4567890-1234567",
        email_id="m1", order_date=date(2026, 5, 12), total_cents=4723,
        raw_summary="x", raw_payload={},
    )
    storage.mark_order_categorized(db, oid, chosen_category="cat-baby")

    mock_y = MagicMock()
    mock_y.list_uncategorized.return_value = [{
        "ynab_txn_id": "tx-1", "ynab_account_id": "acc",
        "payee": "AMAZON.COM*ABC123", "amount_cents": -4723,
        "txn_date": date(2026, 5, 14), "memo": "",
    }]
    mock_y.list_categories.return_value = [{"id": "cat-baby", "name": "Baby", "group": "Family"}]
    mock_cls.return_value = mock_y

    poll_once(_settings(tmp_path))

    mock_y.set_category.assert_called_once_with("tx-1", "cat-baby")
    assert storage.list_unmatched_amazon_orders(db) == []


@patch("bot.ynab_watcher.YnabClient")
def test_enqueues_non_amazon_txn_for_digest(mock_cls, tmp_path):
    db = tmp_path / "test.db"
    storage.init_db(db)

    mock_y = MagicMock()
    mock_y.list_uncategorized.return_value = [{
        "ynab_txn_id": "tx-2", "ynab_account_id": "acc",
        "payee": "STARBUCKS #1234", "amount_cents": -1275,
        "txn_date": date(2026, 5, 14), "memo": "",
    }]
    mock_y.list_categories.return_value = []
    mock_cls.return_value = mock_y

    poll_once(_settings(tmp_path))

    with storage.connect(db) as con:
        rows = con.execute("SELECT * FROM pending_txn").fetchall()
        assert len(rows) == 1
        assert rows[0]["payee"] == "STARBUCKS #1234"
```

- [ ] **Step 2: Run tests, verify they fail**

Expected: ImportError on `bot.ynab_watcher`.

- [ ] **Step 3: Implement `bot/ynab_watcher.py`**

```python
"""YNAB poller: match new charges to pending orders, enqueue the rest."""
from __future__ import annotations

import logging
import re

from bot import storage
from bot.config import Settings
from bot.matcher import find_best_match
from bot.ynab_client import YnabClient

log = logging.getLogger(__name__)

PAYEE_AMAZON_RE = re.compile(r"\bAMAZON", re.I)
PAYEE_VENMO_RE = re.compile(r"\bVENMO", re.I)


def _is_amazon_or_venmo(payee: str) -> str | None:
    if PAYEE_AMAZON_RE.search(payee or ""):
        return "amazon"
    if PAYEE_VENMO_RE.search(payee or ""):
        return "venmo"
    return None


def poll_once(settings: Settings) -> dict:
    """Returns {matched: N, enqueued: M}."""
    storage.init_db(settings.paths.database)
    ynab = YnabClient(settings.ynab_token, settings.ynab.budget_id)

    txns = ynab.list_uncategorized()
    pending_orders = storage.list_unmatched_amazon_orders(settings.paths.database)

    matched = 0
    enqueued = 0

    for txn in txns:
        source = _is_amazon_or_venmo(txn["payee"])
        if source in {"amazon", "venmo"}:
            candidates = [o for o in pending_orders if o["source"] == source]
            best = find_best_match(candidates, txn, source=source)
            if best is None:
                # No clean match — fall through to digest enqueue
                _enqueue(settings, txn)
                enqueued += 1
                continue
            category_id = best["chosen_category"]
            if not category_id:
                continue  # Order was queued but user hasn't chosen yet
            try:
                ynab.set_category(txn["ynab_txn_id"], category_id)
                storage.record_match(
                    settings.paths.database,
                    pending_order_id=best["id"],
                    ynab_txn_id=txn["ynab_txn_id"],
                )
                storage.audit(
                    settings.paths.database, "matched",
                    {"order_id": best["id"], "txn": txn["ynab_txn_id"]},
                )
                matched += 1
            except Exception as e:
                log.error("set_category failed: %s", e)
        else:
            _enqueue(settings, txn)
            enqueued += 1

    # Expire orders older than 30 days that never matched
    expired = _expire_stale_orders(settings.paths.database, days=30)

    storage.audit(settings.paths.database, "ynab_poll",
                  {"matched": matched, "enqueued": enqueued, "expired": expired})
    return {"matched": matched, "enqueued": enqueued, "expired": expired}


def _expire_stale_orders(db_path, *, days: int = 30) -> int:
    """Transition categorized-but-never-matched orders to status='expired' after N days."""
    from datetime import datetime, timedelta
    cutoff = datetime.utcnow() - timedelta(days=days)
    with storage.connect(db_path) as con:
        cur = con.execute(
            """UPDATE pending_order
               SET status = 'expired', updated_at = ?
               WHERE status = 'categorized'
                 AND id NOT IN (SELECT pending_order_id FROM matched_charge)
                 AND created_at < ?""",
            (datetime.utcnow(), cutoff),
        )
        return cur.rowcount


def _enqueue(settings: Settings, txn: dict) -> None:
    storage.insert_pending_txn(
        settings.paths.database,
        user_id=settings.gmail_accounts[0].user_id,  # MVP-1 single user
        ynab_txn_id=txn["ynab_txn_id"],
        ynab_account_id=txn["ynab_account_id"],
        payee=txn["payee"],
        amount_cents=txn["amount_cents"],
        txn_date=txn["txn_date"],
        memo=txn["memo"],
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    from bot.config import load_settings
    result = poll_once(load_settings())
    log.info("ynab_watcher: %s", result)
```

- [ ] **Step 4: Run tests, verify they pass**

```powershell
pytest tests/test_ynab_watcher.py -v
```

Expected: 2 PASS.

- [ ] **Step 5: Commit**

```powershell
git add bot/ynab_watcher.py tests/test_ynab_watcher.py
git commit -m "feat(ynab-watcher): match categorized orders to charges + enqueue rest"
```

---

## Phase 5: Telegram bot

### Task 12: Conversation state machine

Pure logic for "what should the bot ask next?" given the current queue. Isolated from Telegram so it's testable.

**Files:**
- Create: `bot/conversation.py`
- Test: `tests/test_conversation.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_conversation.py
from datetime import date
from bot import storage
from bot.conversation import next_item_for_user, format_item_prompt


def test_next_item_returns_oldest_pending(tmp_path):
    db = tmp_path / "test.db"
    storage.init_db(db)
    storage.insert_pending_order(
        db, user_id="steven", source="amazon", external_id="A",
        email_id="m1", order_date=date(2026,5,12), total_cents=4723,
        raw_summary="diapers", raw_payload={"summary":"diapers","items":["Diapers"]},
    )
    storage.insert_pending_order(
        db, user_id="steven", source="amazon", external_id="B",
        email_id="m2", order_date=date(2026,5,13), total_cents=2200,
        raw_summary="batteries", raw_payload={"summary":"batteries","items":["AA"]},
    )

    item = next_item_for_user(db, user_id="steven")
    assert item is not None
    assert item["kind"] == "order"
    assert item["raw_summary"] == "diapers"


def test_format_item_prompt_amazon():
    item = {
        "kind": "order", "source": "amazon",
        "total_cents": 4723, "order_date": date(2026,5,12),
        "raw_summary": "3 items: Diapers, Wipes, Formula",
        "suggested_category_name": "Baby Supplies",
    }
    msg = format_item_prompt(item)
    assert "$47.23" in msg
    assert "Amazon" in msg
    assert "Baby Supplies" in msg
```

- [ ] **Step 2: Run tests, verify they fail**

```powershell
pytest tests/test_conversation.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement `bot/conversation.py`**

```python
"""Pure conversation logic — what to ask the user next, and how to format it.

Has zero Telegram dependency. The telegram_bot module calls these functions
and renders the results.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
import json

from bot import storage


def next_item_for_user(db_path: Path | str, *, user_id: str) -> dict | None:
    """Return the next item to ask the user about, or None if the queue is empty.

    Priority: pending_orders (real-time email items) before pending_txn (digest items).
    Within each, oldest first.
    """
    with storage.connect(db_path) as con:
        order = con.execute(
            """SELECT * FROM pending_order
               WHERE user_id = ? AND status = 'pending'
               ORDER BY id LIMIT 1""", (user_id,)
        ).fetchone()
        if order:
            d = dict(order)
            d["kind"] = "order"
            d["raw_payload"] = json.loads(d.get("raw_payload") or "{}")
            return d

        txn = con.execute(
            """SELECT * FROM pending_txn
               WHERE user_id = ? AND status = 'pending'
               ORDER BY id LIMIT 1""", (user_id,)
        ).fetchone()
        if txn:
            d = dict(txn)
            d["kind"] = "txn"
            return d

    return None


def format_item_prompt(item: dict) -> str:
    """Plain-text message body. Inline keyboard rendered separately."""
    if item["kind"] == "order":
        source = item["source"]
        emoji = "🛒" if source == "amazon" else "💸"
        amount = item["total_cents"] / 100
        date_str = item["order_date"].strftime("%-m/%-d") if hasattr(item["order_date"], "strftime") else str(item["order_date"])
        summary = item.get("raw_summary", "")
        suggestion = item.get("suggested_category_name") or "(no suggestion)"
        return (
            f"{emoji} {source.title()} — ${amount:.2f} · {date_str}\n"
            f"{summary}\n\n"
            f"Best guess: {suggestion}"
        )
    else:  # txn
        amount = abs(item["amount_cents"]) / 100
        date_str = str(item.get("txn_date", ""))
        payee = item.get("payee", "?")
        suggestion = item.get("suggested_category_name") or "(no suggestion)"
        return (
            f"📋 ${amount:.2f} {payee} · {date_str}\n\n"
            f"Best guess: {suggestion}"
        )
```

- [ ] **Step 4: Run tests, verify they pass**

```powershell
pytest tests/test_conversation.py -v
```

Expected: PASS.  *Note: `strftime("%-m/%-d")` is POSIX; on Windows use `"%#m/%#d"`. Adjust if test fails on Windows.*

- [ ] **Step 5: Commit**

```powershell
git add bot/conversation.py tests/test_conversation.py
git commit -m "feat(conversation): next_item + format_item_prompt"
```

---

### Task 13: Telegram bot — bootstrap + message dispatch

Long-running `Application` with three handlers: `/start`, slash commands, and free-text replies. On startup it spawns a background task that watches for new pending items and pushes them. On user reply it parses, updates state, and pushes the next item.

**Files:**
- Create: `bot/telegram_bot.py`
- Test: `tests/test_telegram_bot.py` (smoke test only — bot loop is hard to unit-test fully)

- [ ] **Step 1: Write a smoke test**

```python
# tests/test_telegram_bot.py
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from bot.telegram_bot import _parse_user_reply


def test_parse_reply_confirmation():
    assert _parse_user_reply("y") == ("confirm", None)
    assert _parse_user_reply("YES") == ("confirm", None)
    assert _parse_user_reply("✅") == ("confirm", None)


def test_parse_reply_skip():
    assert _parse_user_reply("skip") == ("skip", None)
    assert _parse_user_reply("/skip") == ("skip", None)


def test_parse_reply_undo():
    assert _parse_user_reply("/undo") == ("undo", None)


def test_parse_reply_freetext():
    assert _parse_user_reply("for the trip") == ("text", "for the trip")
    assert _parse_user_reply("Groceries") == ("text", "Groceries")
```

- [ ] **Step 2: Run test, verify it fails**

```powershell
pytest tests/test_telegram_bot.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement `bot/telegram_bot.py`**

```python
"""Long-running Telegram bot.

Architecture:
  - python-telegram-bot Application with handlers for /start, slash commands, and
    free-text messages.
  - Background asyncio task polls the pending queue every N seconds and pushes
    new items to the user with inline keyboards.
  - User reply is parsed (confirm / category name / free text / slash command)
    and the chosen category is set on the pending row. The matcher then applies
    it to YNAB on the next ynab_watcher run.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, time as dtime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters,
    ContextTypes,
)

from bot import storage
from bot.config import Settings, load_settings
from bot.conversation import next_item_for_user, format_item_prompt
from bot.ynab_client import YnabClient

log = logging.getLogger(__name__)

CONFIRM_WORDS = {"y", "yes", "✅", "✓", "yep", "yeah", "ok"}
NO_WORDS = {"n", "no", "nope"}


def _parse_user_reply(text: str) -> tuple[str, str | None]:
    t = text.strip().lower()
    if t in CONFIRM_WORDS:
        return ("confirm", None)
    if t in NO_WORDS:
        return ("reject", None)
    if t in {"skip", "/skip"}:
        return ("skip", None)
    if t in {"undo", "/undo"}:
        return ("undo", None)
    if t.startswith("/"):
        return ("command", t)
    return ("text", text.strip())


def _build_keyboard(suggestion: str | None, top_categories: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    if suggestion:
        rows.append([InlineKeyboardButton(f"✅ {suggestion}", callback_data=f"cat:{suggestion}")])
    chips = [InlineKeyboardButton(c["name"], callback_data=f"cat:{c['id']}") for c in top_categories[:3]]
    if chips:
        rows.append(chips)
    rows.append([
        InlineKeyboardButton("📂 Other...", callback_data="other"),
        InlineKeyboardButton("⏭ Skip", callback_data="skip"),
    ])
    return InlineKeyboardMarkup(rows)


async def _push_next_item(app: Application, settings: Settings, chat_id: int, user_id: str):
    item = next_item_for_user(settings.paths.database, user_id=user_id)
    if not item:
        await app.bot.send_message(chat_id, "All caught up. ✨")
        return
    # Look up category name for suggestion
    ynab = YnabClient(settings.ynab_token, settings.ynab.budget_id)
    cats = ynab.list_categories()
    by_id = {c["id"]: c for c in cats}
    suggested_name = None
    if item.get("suggested_category"):
        suggested_name = by_id.get(item["suggested_category"], {}).get("name")
    item["suggested_category_name"] = suggested_name

    msg = format_item_prompt(item)
    kb = _build_keyboard(suggested_name, cats)
    await app.bot.send_message(chat_id, msg, reply_markup=kb)

    # Update bot_conversation state
    with storage.connect(settings.paths.database) as con:
        con.execute(
            """INSERT INTO bot_conversation (chat_id, user_id, last_asked_kind, last_asked_id, last_action_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET
                 last_asked_kind=excluded.last_asked_kind,
                 last_asked_id=excluded.last_asked_id,
                 last_action_at=excluded.last_action_at""",
            (chat_id, user_id, item["kind"], item["id"], datetime.utcnow()),
        )


async def _handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    settings: Settings = ctx.application.bot_data["settings"]
    chat_id = update.effective_chat.id
    user_id = ctx.application.bot_data["user_id_by_chat"].get(chat_id)
    if not user_id:
        await update.message.reply_text("Not authorized. (chat_id not in config)")
        return

    kind, payload = _parse_user_reply(update.message.text)
    if kind == "confirm":
        await _apply_choice(ctx.application, settings, chat_id, user_id, action="confirm")
    elif kind == "skip":
        await _apply_choice(ctx.application, settings, chat_id, user_id, action="skip")
    elif kind == "undo":
        await update.message.reply_text("Undo not yet implemented (MVP-1.1).")
    elif kind == "text":
        await _apply_choice(ctx.application, settings, chat_id, user_id,
                             action="text", text=payload)
    await _push_next_item(ctx.application, settings, chat_id, user_id)


async def _handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    settings: Settings = ctx.application.bot_data["settings"]
    chat_id = update.effective_chat.id
    user_id = ctx.application.bot_data["user_id_by_chat"].get(chat_id)
    if not user_id:
        return
    data = update.callback_query.data
    await update.callback_query.answer()
    if data.startswith("cat:"):
        category_ref = data[4:]  # name (for suggestion confirm) or id (for chip)
        await _apply_choice(ctx.application, settings, chat_id, user_id,
                             action="text", text=category_ref)
    elif data == "skip":
        await _apply_choice(ctx.application, settings, chat_id, user_id, action="skip")
    elif data == "other":
        await update.callback_query.message.reply_text(
            "Reply with the category name or a short description."
        )
        return
    await _push_next_item(ctx.application, settings, chat_id, user_id)


async def _apply_choice(app, settings: Settings, chat_id: int, user_id: str,
                        *, action: str, text: str | None = None):
    """Resolve user input to a category id and persist."""
    # Look up current state
    with storage.connect(settings.paths.database) as con:
        conv = con.execute(
            "SELECT last_asked_kind, last_asked_id FROM bot_conversation WHERE chat_id=?",
            (chat_id,)
        ).fetchone()
        if not conv:
            return
        kind, item_id = conv["last_asked_kind"], conv["last_asked_id"]

    ynab = YnabClient(settings.ynab_token, settings.ynab.budget_id)
    cats = ynab.list_categories()
    cat_id = None

    if action == "skip":
        if kind == "txn":
            with storage.connect(settings.paths.database) as con:
                con.execute("UPDATE pending_txn SET status='skipped' WHERE id=?", (item_id,))
        return

    if action == "confirm":
        with storage.connect(settings.paths.database) as con:
            row = con.execute(
                f"SELECT suggested_category FROM pending_{'order' if kind=='order' else 'txn'} WHERE id=?",
                (item_id,),
            ).fetchone()
            cat_id = row["suggested_category"] if row else None

    elif action == "text" and text:
        # Try exact match by category id first (callback data from chip)
        if any(c["id"] == text for c in cats):
            cat_id = text
        else:
            # Try exact category name (case-insensitive)
            for c in cats:
                if c["name"].lower() == text.lower():
                    cat_id = c["id"]
                    break
            if not cat_id:
                # Free-text → categorizer disambiguation
                from bot.categorizer import Categorizer
                cat_engine = Categorizer(settings.ollama.endpoint, settings.ollama.model)
                suggestion = cat_engine.suggest(
                    summary=text, amount_cents=0, date_str="", source="freetext",
                    categories=cats,
                )
                cat_id = suggestion.get("category_id")

    if not cat_id:
        await app.bot.send_message(chat_id, "Couldn't resolve that to a category. Try again or tap a button.")
        return

    if kind == "order":
        storage.mark_order_categorized(settings.paths.database, item_id, chosen_category=cat_id)
    else:
        with storage.connect(settings.paths.database) as con:
            con.execute(
                "UPDATE pending_txn SET chosen_category=?, chosen_at=?, status='categorized' WHERE id=?",
                (cat_id, datetime.utcnow(), item_id),
            )
        # Apply to YNAB immediately for non-Amazon/Venmo (they're already in YNAB)
        with storage.connect(settings.paths.database) as con:
            row = con.execute("SELECT ynab_txn_id FROM pending_txn WHERE id=?", (item_id,)).fetchone()
            if row:
                try:
                    ynab.set_category(row["ynab_txn_id"], cat_id)
                except Exception as e:
                    log.error("set_category failed: %s", e)

    storage.audit(settings.paths.database, "categorized",
                  {"kind": kind, "item_id": item_id, "category_id": cat_id})


async def _start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Hi. Your chat id is {update.effective_chat.id} — add it to config.yaml."
    )


async def _pending_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    settings: Settings = ctx.application.bot_data["settings"]
    with storage.connect(settings.paths.database) as con:
        n_o = con.execute("SELECT COUNT(*) c FROM pending_order WHERE status='pending'").fetchone()["c"]
        n_t = con.execute("SELECT COUNT(*) c FROM pending_txn WHERE status='pending'").fetchone()["c"]
    await update.message.reply_text(f"{n_o} pending orders, {n_t} pending txns.")


def _in_quiet_hours(now: datetime, window: str) -> bool:
    """window format: 'HH:MM-HH:MM' (e.g., '22:00-07:00'). Handles overnight wrap."""
    start_s, end_s = window.split("-")
    start = dtime.fromisoformat(start_s)
    end = dtime.fromisoformat(end_s)
    t = now.time()
    if start <= end:
        return start <= t < end
    return t >= start or t < end  # overnight wrap


async def _push_loop(app: Application):
    """Background task: every 30s, push next item to each known chat.
    Respects quiet hours — queues silently, delivers on wake."""
    while True:
        try:
            settings: Settings = app.bot_data["settings"]
            if _in_quiet_hours(datetime.now(), settings.telegram.quiet_hours):
                await asyncio.sleep(60)
                continue
            for chat_id, user_id in app.bot_data["user_id_by_chat"].items():
                await _push_next_item(app, settings, chat_id, user_id)
        except Exception as e:
            log.error("push loop error: %s", e)
        await asyncio.sleep(30)


def run():
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    app = Application.builder().token(settings.telegram_bot_token).build()

    app.bot_data["settings"] = settings
    app.bot_data["user_id_by_chat"] = {a.chat_id: a.user_id for a in settings.gmail_accounts}

    app.add_handler(CommandHandler("start", _start_cmd))
    app.add_handler(CommandHandler("pending", _pending_cmd))
    app.add_handler(CallbackQueryHandler(_handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_text))

    async def post_init(application):
        application.create_task(_push_loop(application))

    app.post_init = post_init
    app.run_polling()


if __name__ == "__main__":
    run()
```

- [ ] **Step 4: Run tests, verify they pass**

```powershell
pytest tests/test_telegram_bot.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add bot/telegram_bot.py tests/test_telegram_bot.py
git commit -m "feat(telegram-bot): long-polling bot with push loop, callbacks, free-text routing"
```

---

## Phase 6: Deployment

### Task 14: First-run setup script

Interactive script run once on the home desktop to capture: YNAB token, YNAB budget id, Telegram chat id (by asking user to message the bot), then writes `.env` + finishes `config.yaml`.

**Files:**
- Create: `scripts/first_run_setup.py`

- [ ] **Step 1: Implement**

```python
"""One-time setup on the deployment machine.

Captures:
  - YNAB Personal Access Token (write to .env)
  - YNAB budget_id (pick from list)
  - Telegram chat_id (start bot, ask user to message it, capture incoming chat_id)
  - Updates config.yaml with the captured values
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

import yaml
from telegram.ext import Application, MessageHandler, filters
from telegram import Update
import ynab

REPO = Path(__file__).resolve().parent.parent
CONFIG_EXAMPLE = REPO / "config.yaml.example"
CONFIG = REPO / "config.yaml"
ENV = REPO / ".env"


def ensure_config():
    if not CONFIG.exists():
        shutil.copy(CONFIG_EXAMPLE, CONFIG)
        print(f"Created {CONFIG} from example.")


def capture_ynab():
    token = input("YNAB Personal Access Token (https://app.ynab.com/settings): ").strip()
    if not token:
        sys.exit("Token required.")

    config = ynab.Configuration(access_token=token)
    with ynab.ApiClient(config) as api_client:
        api = ynab.BudgetsApi(api_client)
        budgets = api.get_budgets().data.budgets
    print("\nYour budgets:")
    for i, b in enumerate(budgets):
        print(f"  [{i}] {b.name}  ({b.id})")
    idx = int(input("Pick budget number: "))
    budget_id = budgets[idx].id
    return token, budget_id


async def capture_telegram_chat_id():
    token = input("Telegram bot token (from BotFather): ").strip()
    if not token:
        sys.exit("Token required.")

    print("Now message your bot from your phone (any text). Waiting...")
    chat_id_holder = {}

    async def on_msg(update: Update, ctx):
        chat_id_holder["id"] = update.effective_chat.id
        await update.message.reply_text(f"Got it. Your chat id is {update.effective_chat.id}.")
        await ctx.application.stop()

    app = Application.builder().token(token).build()
    app.add_handler(MessageHandler(filters.ALL, on_msg))
    await app.run_polling(close_loop=False)

    return token, chat_id_holder.get("id")


def write_env(ynab_token: str, telegram_token: str):
    lines = [f"YNAB_TOKEN={ynab_token}\n", f"TELEGRAM_BOT_TOKEN={telegram_token}\n"]
    ENV.write_text("".join(lines))
    print(f"Wrote {ENV}")


def update_config(budget_id: str, chat_id: int):
    data = yaml.safe_load(CONFIG.read_text())
    data["ynab"]["budget_id"] = budget_id
    data["gmail_accounts"][0]["chat_id"] = int(chat_id)
    CONFIG.write_text(yaml.safe_dump(data, sort_keys=False))
    print(f"Updated {CONFIG}")


def main():
    ensure_config()
    ynab_token, budget_id = capture_ynab()
    telegram_token, chat_id = asyncio.run(capture_telegram_chat_id())
    if not chat_id:
        sys.exit("Did not receive chat_id — exiting.")
    write_env(ynab_token, telegram_token)
    update_config(budget_id, chat_id)
    print("\nNext: run scripts/setup_scheduled_tasks.ps1 (admin PowerShell) to install schedulers.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Commit**

```powershell
git add scripts/first_run_setup.py
git commit -m "feat(setup): first-run interactive script for YNAB + Telegram + config"
```

---

### Task 15: Windows scheduled tasks setup

**Files:**
- Create: `scripts/setup_scheduled_tasks.ps1`

- [ ] **Step 1: Implement**

```powershell
# scripts/setup_scheduled_tasks.ps1 — run as administrator
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
)

$python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "venv python not found at $python — run 'python -m venv .venv' and install dependencies first." }

function New-YnabTask {
    param([string]$Name, [string]$Script, [int]$IntervalMinutes)
    $action = New-ScheduledTaskAction -Execute $python -Argument "-m bot.$Script" -WorkingDirectory $RepoRoot
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest -Force | Out-Null
    Write-Host "Registered: $Name (every $IntervalMinutes min)"
}

New-YnabTask -Name "YNAB-Helper-GmailWatcher" -Script "gmail_watcher" -IntervalMinutes 5
New-YnabTask -Name "YNAB-Helper-YnabWatcher"  -Script "ynab_watcher"  -IntervalMinutes 30

# Long-running bot — separate task, runs at logon, restarts on failure
$action = New-ScheduledTaskAction -Execute $python -Argument "-m bot.telegram_bot" -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 9999)
Register-ScheduledTask -TaskName "YNAB-Helper-Bot" -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest -Force | Out-Null
Write-Host "Registered: YNAB-Helper-Bot (long-running)"

Write-Host "`nDone. Manage tasks via Task Scheduler GUI (taskschd.msc) under 'Task Scheduler Library'."
```

- [ ] **Step 2: Commit**

```powershell
git add scripts/setup_scheduled_tasks.ps1
git commit -m "feat(deploy): PowerShell script to register scheduled tasks"
```

---

### Task 16: README rewrite + deployment guide

**Files:**
- Modify: `README.md` (full rewrite — current README is the Chrome-extension one)

- [ ] **Step 1: Replace `README.md`**

```markdown
# YNAB Helper

Personal Telegram assistant that categorizes YNAB transactions while purchases are still fresh in memory. Pulls Amazon and Venmo receipts from Gmail in real time, asks for daily review on everything else, suggests categories using a local LLM (Ollama), and writes the chosen category back to YNAB via API.

See `docs/superpowers/specs/2026-05-16-ynab-helper-design.md` for the full design.

## Status

- MVP-1 (Steven's flow): in development per `docs/superpowers/plans/2026-05-16-ynab-helper-mvp1.md`
- MVP-2 (wife's Gmail): planned
- MVP-3 (weekly digest): planned

## Architecture

- **gmail_watcher** (scheduled, every 5 min) — polls Gmail for new Amazon/Venmo emails, parses, queues
- **ynab_watcher** (scheduled, every 30 min) — matches pending categorized orders to new YNAB charges, queues non-Amazon/Venmo for daily digest
- **telegram_bot** (long-running) — pushes items to user, parses replies, applies categories
- **categorizer** (Ollama HTTP) — local LLM suggests YNAB category from items + history
- **matcher** — pure scoring function linking emails to YNAB charges

## Requirements (deployment machine)

- Windows 10+, Python 3.12+
- Ollama installed and a model pulled (e.g. `ollama pull qwen2.5:14b`)
- Telegram bot created via BotFather (free)
- YNAB Personal Access Token
- Gmail account with OAuth approved for the bot

## Setup

```powershell
git clone <repo>
cd ynab-helper
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# Auth Gmail (browser popup):
python scripts\reauth_gmail.py

# Validate parsing assumptions against your actual inbox:
python scripts\inspect_receipts.py

# Capture YNAB + Telegram tokens, pick budget, get chat_id:
python scripts\first_run_setup.py

# Install Windows scheduled tasks (admin PowerShell):
.\scripts\setup_scheduled_tasks.ps1

# Verify Ollama running:
ollama list
```

## Running

After `setup_scheduled_tasks.ps1`:
- `YNAB-Helper-GmailWatcher` runs every 5 min
- `YNAB-Helper-YnabWatcher` runs every 30 min
- `YNAB-Helper-Bot` runs at logon and restarts on failure

Check via `taskschd.msc`.

## Testing

```powershell
pytest -v
```

## Chrome extension (backfill tool)

The original Chrome scraper lives in `chrome-extension/` — useful for catching up on historical Amazon orders that the bot wasn't around to see. See `chrome-extension/README.md`.

## Files

- `bot/` — Python source
- `tests/` — unit tests + fixtures
- `scripts/` — setup, deployment, validation
- `chrome-extension/` — legacy Chrome backfill tool
- `docs/superpowers/specs/` — design + parsing-knowledge docs
- `docs/superpowers/plans/` — implementation plans

## Privacy

All processing happens on the local machine. The local Ollama instance is the only AI involved — no transaction data leaves your network. The Gmail OAuth token is stored locally. The YNAB API token is in a gitignored `.env`.
```

- [ ] **Step 2: Commit**

```powershell
git add README.md
git commit -m "docs: rewrite README for Python bot deployment + extension as backfill"
```

---

## Phase 7: End-to-end validation

### Task 17: Smoke test on the home desktop

This is the final task — proves the whole stack works end-to-end before declaring MVP-1 done.

- [ ] **Step 1: Push to git from work laptop, pull on home desktop**

```powershell
# Work laptop
git push origin master

# Home desktop
cd <repo path>
git pull
```

- [ ] **Step 2: Install on home desktop**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

- [ ] **Step 3: Install + start Ollama (if not already)**

Download from https://ollama.com/download, then:

```powershell
ollama pull qwen2.5:14b
ollama list  # verify
```

- [ ] **Step 4: Run setup**

```powershell
python scripts\reauth_gmail.py        # OAuth in browser
python scripts\inspect_receipts.py    # verify inbox patterns
python scripts\first_run_setup.py     # YNAB + Telegram + config
```

- [ ] **Step 5: Run watchers manually before scheduling**

```powershell
python -m bot.gmail_watcher
python -m bot.ynab_watcher
```

Expected: both log new pending items inserted (if any present in inbox / YNAB).

- [ ] **Step 6: Start the bot**

```powershell
python -m bot.telegram_bot
```

Expected: bot starts long-polling, pushes any pending items to you on Telegram within 30 seconds.

- [ ] **Step 7: Categorize one Amazon order end-to-end**

Place a small (≤$5) Amazon order to trigger the full flow:
1. Within ~10 min, bot DMs you the order with line items + suggestion
2. Reply `y` to confirm
3. Wait 1–7 days for the charge to clear in YNAB
4. Within 30 min of the charge appearing, ynab_watcher applies the chosen category
5. Verify in YNAB UI that the transaction has the category set

- [ ] **Step 8: Install as scheduled tasks**

```powershell
# Admin PowerShell
.\scripts\setup_scheduled_tasks.ps1
```

- [ ] **Step 9: Final commit — tag MVP-1**

```powershell
git tag -a mvp-1 -m "MVP-1: single-user Amazon + Venmo email flow + daily digest, deployed and verified"
git push --tags
```

---

## What's NOT in this plan (deferred to MVP-2, MVP-3, or v2)

- Wife's Gmail account onboarding (`scripts/add_gmail_account.py` and multi-chat-id routing) — **MVP-2**
- Weekly discretionary-category digest (`bot/reporters/weekly_digest.py`) — **MVP-3**
- `/undo` (5-min revert) — TODO comment in Task 13, will add when needed
- Subscribe & Save recurring detection — never (treated as ordinary in spec)
- Refund auto-linking — out of spec scope
- Multi-currency — out of spec scope
- NSSM service install (alternative to scheduled-task restart) — design notes only, Task 15 uses scheduled-task with restart-on-failure
- HTML email push notifications via Gmail watch + Pub/Sub — possible v2 optimization (cheaper polling)
