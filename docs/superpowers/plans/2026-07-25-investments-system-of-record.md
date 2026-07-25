# Investments & Insurance System of Record — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move retirement/investment and insurance tracking out of the Google Sheet → xlsx → parse-on-read pipeline and into the SQLite DB, with in-app editing.

**Architecture:** Seven new tables in `bot/storage.py`. Three new bot modules — `investments_store.py` (holdings, rounds, values, snapshot assembly), `investments_insurance.py` (policies, premiums, drift), `investments_import.py` (one-time xlsx → DB). `GET /investments/snapshot` keeps its exact JSON shape but is served from the DB, so the four existing Tauri pages need no changes; two new editor pages are added alongside them.

**Tech Stack:** Python 3.12, SQLite (stdlib `sqlite3`), FastAPI + Pydantic, pytest, openpyxl (import only). UI: Tauri 2, React, TanStack Query, Tailwind.

**Spec:** `docs/superpowers/specs/2026-07-25-investments-system-of-record-design.md`

## Global Constraints

- **Money is always integer cents**, column suffix `_cents`. Never floats.
- **Domain entities use TEXT primary keys** (uuid4 hex); logs and observation rows use `INTEGER PRIMARY KEY AUTOINCREMENT`.
- **All DB access goes through `bot/storage.py`'s `connect()` context manager.** It sets `PRAGMA foreign_keys = ON`, `row_factory = sqlite3.Row`, and commits on clean exit.
- **New tables go in the `SCHEMA` string** as `CREATE TABLE IF NOT EXISTS`. Do **not** add them to `_migrate()` — that function is only for additive `ALTER TABLE` on pre-existing tables.
- **Pydantic body models live at module scope** in `http_api.py`, next to `YnabPushBody` (~line 199). A body model defined inside `build_app()` makes every POST return 422.
- **Every route** carries `dependencies=[Depends(_require_token)]`. **Every mutation** calls `storage.audit(db_path, "<event>", {...})`.
- **No DELETE routes.** Soft flags only: `holding.closed`, `insurance_policy.active`.
- **Bot restart required** for schema + route changes to go live. The bot is the `YNAB-Helper-Bot` scheduled task — never PID-kill or start it directly.
- **Tauri IPC:** JS argument keys must be camelCase; never pass `serde_json::Value` as a command arg.
- **Test command:** `python -m pytest tests/<file> -v` from the repo root `C:\Users\Steven\ynabhelper`.
- **Live DB is local:** `C:/Users/Steven/ynabhelper/ynab_helper.db`. Never query the stale copy on `G:`.

**Documented refinement to the spec:** the `holding` table gains one column beyond the spec, `account_type TEXT` — the sheet's free-text label ("401k", "Roth IRA", "529"). Without it the import would discard that column, and the existing `Holding` TS interface already has an `account_type` field to fill. `kind` remains the machine-readable class.

## File Structure

**Create:**
- `bot/investments_store.py` — rounds, holdings, values, snapshot assembly, totals
- `bot/investments_insurance.py` — policies, observed premiums, drift
- `bot/investments_import.py` — one-time xlsx → DB import
- `scripts/verify_investments_import.py` — diffs xlsx parse against DB snapshot
- `tests/test_investments_store.py`
- `tests/test_investments_insurance.py`
- `tests/test_investments_import.py`
- `ynabhelper-ui/src/pages/InvestmentsUpdate.tsx`
- `ynabhelper-ui/src/pages/InvestmentsInsuranceEdit.tsx`

**Modify:**
- `bot/storage.py` — `SCHEMA` string, eight new tables
- `bot/http_api.py` — module-scope body models (~line 199); routes in the Investments block (~line 796)
- `bot/investments.py` — unchanged code, demoted to import-only use
- `ynabhelper-ui/src/lib/types.ts` — new interfaces after line 333
- `ynabhelper-ui/src/lib/api.ts` — new client functions after line 224
- `ynabhelper-ui/src/App.tsx` — two routes + two sidebar entries
- `ynabhelper-ui/src/pages/InvestmentsOverview.tsx` — round selector + as-of chip
- `ynabhelper-ui/src/pages/InvestmentsHoldings.tsx`, `InvestmentsAllocation.tsx`, `InvestmentsInsurance.tsx` — as-of chip

---

## Task 1: Schema

**Files:**
- Modify: `bot/storage.py` (the `SCHEMA` string, append before its closing `"""`)
- Test: `tests/test_investments_store.py`

**Interfaces:**
- Consumes: `storage.init_db(db_path)`, `storage.connect(db_path)`
- Produces: tables `holding`, `snapshot_round`, `holding_value`, `property_detail`, `insurance_policy`, `insurance_premium_observed`, `savings_target`

- [ ] **Step 1: Write the failing test**

Create `tests/test_investments_store.py`:

```python
import sqlite3
from datetime import date

import pytest

from bot.storage import init_db, connect


INVESTMENT_TABLES = {
    "holding",
    "snapshot_round",
    "holding_value",
    "property_detail",
    "insurance_policy",
    "insurance_premium_observed",
    "savings_target",
}


def test_init_creates_investment_tables(tmp_path):
    db = tmp_path / "test.db"
    init_db(db)
    con = sqlite3.connect(db)
    tables = {
        r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert INVESTMENT_TABLES.issubset(tables)


def test_holding_value_unique_per_holding_round(tmp_path):
    db = tmp_path / "test.db"
    init_db(db)
    with connect(db) as con:
        con.execute(
            "INSERT INTO holding (id, name, kind) VALUES ('h1', 'Marcus', 'cash')"
        )
        con.execute(
            "INSERT INTO snapshot_round (id, label, as_of_date) "
            "VALUES ('r1', 'Jul 2026', '2026-07-25')"
        )
        con.execute(
            "INSERT INTO holding_value (holding_id, round_id, as_of_date, value_cents) "
            "VALUES ('h1', 'r1', '2026-07-25', 2566800)"
        )
    with pytest.raises(sqlite3.IntegrityError):
        with connect(db) as con:
            con.execute(
                "INSERT INTO holding_value (holding_id, round_id, as_of_date, value_cents) "
                "VALUES ('h1', 'r1', '2026-07-25', 999)"
            )
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_investments_store.py -v`
Expected: FAIL — `assert INVESTMENT_TABLES.issubset(tables)` is False.

- [ ] **Step 3: Add the tables to the SCHEMA string**

In `bot/storage.py`, append inside the `SCHEMA` triple-quoted string (before its closing `"""`):

```sql
-- ── Investments & insurance system of record (2026-07-25) ────────────────
-- Replaces the Google Sheet -> xlsx -> parse-on-read pipeline. A `holding`
-- is a periodically-observed number, NOT a budget account: it has no
-- transactions and never enters envelope math. `ledger_account_id` links
-- the two that exist in both worlds (Marcus, Coastal).

CREATE TABLE IF NOT EXISTS holding (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  owner TEXT,
  kind TEXT NOT NULL DEFAULT 'other'
    CHECK (kind IN ('retirement','brokerage','crypto','cash','education','property','other')),
  account_type TEXT,
  institution TEXT,
  account_number TEXT,
  tax_treatment TEXT,
  ledger_account_id TEXT REFERENCES account(id),
  closed INTEGER NOT NULL DEFAULT 0,
  sort_order INTEGER NOT NULL DEFAULT 0,
  notes TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS snapshot_round (
  id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  as_of_date DATE NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (as_of_date)
);

CREATE TABLE IF NOT EXISTS holding_value (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  holding_id TEXT NOT NULL REFERENCES holding(id),
  round_id TEXT NOT NULL REFERENCES snapshot_round(id),
  as_of_date DATE NOT NULL,
  value_cents INTEGER NOT NULL,
  market_value_cents INTEGER,
  debt_cents INTEGER,
  vested_cents INTEGER,
  units REAL,
  unit_price_cents INTEGER,
  source TEXT NOT NULL DEFAULT 'manual'
    CHECK (source IN ('manual','xlsx_import','ledger')),
  is_seeded INTEGER NOT NULL DEFAULT 0,
  note TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (holding_id, round_id)
);

CREATE TABLE IF NOT EXISTS property_detail (
  holding_id TEXT PRIMARY KEY REFERENCES holding(id),
  address TEXT,
  valuation_source TEXT,
  purchase_date DATE,
  is_primary_residence INTEGER NOT NULL DEFAULT 0,
  listed_price_cents INTEGER,
  escrow_cents INTEGER
);

CREATE TABLE IF NOT EXISTS insurance_policy (
  id TEXT PRIMARY KEY,
  insurance_type TEXT NOT NULL,
  provider TEXT,
  policy_number TEXT,
  covers TEXT,
  through_employer INTEGER,
  coverage TEXT,
  deductible TEXT,
  premium_cents INTEGER,
  premium_frequency TEXT NOT NULL DEFAULT 'annual'
    CHECK (premium_frequency IN ('annual','semiannual','quarterly','monthly')),
  paid_via TEXT NOT NULL DEFAULT 'ledger'
    CHECK (paid_via IN ('escrow','ledger','payroll')),
  ledger_payee_norm TEXT,
  sales_contact TEXT,
  renewal_date TEXT,
  comments TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Escrow-paid premiums (Amica home, Fortegra, Neptune) never appear as
-- ledger payees — they're inside the mortgage payment. Hand-entered rows
-- here are the only way that drift becomes visible.
CREATE TABLE IF NOT EXISTS insurance_premium_observed (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  policy_id TEXT NOT NULL REFERENCES insurance_policy(id),
  as_of_date DATE NOT NULL,
  amount_cents INTEGER NOT NULL,
  source TEXT NOT NULL DEFAULT 'manual'
    CHECK (source IN ('escrow','ledger','manual')),
  note TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS savings_target (
  id TEXT PRIMARY KEY,
  effective_year INTEGER NOT NULL UNIQUE,
  age INTEGER,
  combined_salary_cents INTEGER NOT NULL,
  multiplier REAL NOT NULL,
  note TEXT
);

CREATE INDEX IF NOT EXISTS idx_holding_value_round ON holding_value(round_id);
CREATE INDEX IF NOT EXISTS idx_premium_observed_policy
  ON insurance_premium_observed(policy_id, as_of_date);
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_investments_store.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Verify no existing tests broke**

Run: `python -m pytest tests/ -q`
Expected: same pass count as before plus 2.

- [ ] **Step 6: Commit**

```bash
git add bot/storage.py tests/test_investments_store.py
git commit -m "feat(investments): schema for holdings, rounds, values, policies"
```

---

## Task 2: Rounds, holdings, and values store

**Files:**
- Create: `bot/investments_store.py`
- Test: `tests/test_investments_store.py` (append)

**Interfaces:**
- Consumes: `storage.connect`, `storage.init_db`
- Produces:
  - `create_round(db_path, *, label, as_of_date, seed_from_previous=False) -> str`
  - `list_rounds(db_path) -> list[dict]`
  - `latest_round(db_path) -> dict | None`
  - `upsert_holding(db_path, **fields) -> str`
  - `list_holdings(db_path, *, include_closed=True) -> list[dict]`
  - `upsert_values(db_path, *, round_id, values: list[dict]) -> int`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_investments_store.py`:

```python
from bot import investments_store as store


def _seed_holdings(db):
    a = store.upsert_holding(db, name="Marcus", kind="cash", owner="joint")
    b = store.upsert_holding(db, name="Roth IRA", kind="retirement", owner="steven")
    return a, b


def test_create_round_returns_id_and_lists(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    rid = store.create_round(db, label="Jul 2026", as_of_date="2026-07-25")
    rounds = store.list_rounds(db)
    assert len(rounds) == 1
    assert rounds[0]["id"] == rid
    assert rounds[0]["label"] == "Jul 2026"
    assert rounds[0]["as_of_date"] == "2026-07-25"
    assert rounds[0]["value_count"] == 0


def test_seed_from_previous_carries_values_and_marks_them(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    a, b = _seed_holdings(db)
    r1 = store.create_round(db, label="Feb 2026", as_of_date="2026-02-15")
    store.upsert_values(db, round_id=r1, values=[
        {"holding_id": a, "value_cents": 1000, "as_of_date": "2026-02-15"},
        {"holding_id": b, "value_cents": 2000, "as_of_date": "2026-02-15"},
    ])
    r2 = store.create_round(
        db, label="Jul 2026", as_of_date="2026-07-25", seed_from_previous=True,
    )
    with connect(db) as con:
        rows = {
            r["holding_id"]: dict(r) for r in con.execute(
                "SELECT * FROM holding_value WHERE round_id = ?", (r2,)
            )
        }
    assert rows[a]["value_cents"] == 1000
    assert rows[a]["is_seeded"] == 1
    assert rows[a]["as_of_date"] == "2026-07-25"   # round date, not carried
    assert rows[b]["value_cents"] == 2000


def test_upsert_values_updates_and_clears_seeded_flag(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    a, _ = _seed_holdings(db)
    r1 = store.create_round(db, label="Feb 2026", as_of_date="2026-02-15")
    store.upsert_values(db, round_id=r1, values=[
        {"holding_id": a, "value_cents": 1000, "as_of_date": "2026-02-15"},
    ])
    r2 = store.create_round(
        db, label="Jul 2026", as_of_date="2026-07-25", seed_from_previous=True,
    )
    n = store.upsert_values(db, round_id=r2, values=[
        {"holding_id": a, "value_cents": 2566800, "as_of_date": "2026-07-20"},
    ])
    assert n == 1
    with connect(db) as con:
        row = dict(con.execute(
            "SELECT * FROM holding_value WHERE round_id = ? AND holding_id = ?",
            (r2, a),
        ).fetchone())
    assert row["value_cents"] == 2566800
    assert row["is_seeded"] == 0
    assert row["as_of_date"] == "2026-07-20"
    with connect(db) as con:
        count = con.execute(
            "SELECT COUNT(*) FROM holding_value WHERE round_id = ?", (r2,)
        ).fetchone()[0]
    assert count == 1   # upsert, not a second row


def test_upsert_values_stores_components(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    p = store.upsert_holding(db, name="117 Mayfield", kind="property")
    c = store.upsert_holding(db, name="Bitcoin", kind="crypto")
    r = store.create_round(db, label="Jul 2026", as_of_date="2026-07-25")
    store.upsert_values(db, round_id=r, values=[
        {"holding_id": p, "value_cents": 24952292,
         "market_value_cents": 48240000, "debt_cents": 23287708},
        {"holding_id": c, "value_cents": 6072700,
         "units": 0.947, "unit_price_cents": 6411892},
    ])
    with connect(db) as con:
        rows = {
            r_["holding_id"]: dict(r_) for r_ in con.execute(
                "SELECT * FROM holding_value WHERE round_id = ?", (r,)
            )
        }
    assert rows[p]["market_value_cents"] - rows[p]["debt_cents"] == rows[p]["value_cents"]
    assert rows[c]["units"] == 0.947


def test_upsert_holding_updates_when_id_given(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    hid = store.upsert_holding(db, name="Old Name", kind="cash")
    same = store.upsert_holding(db, id=hid, name="New Name", kind="cash", closed=1)
    assert same == hid
    rows = store.list_holdings(db, include_closed=True)
    assert len(rows) == 1
    assert rows[0]["name"] == "New Name"
    assert rows[0]["closed"] == 1
    assert store.list_holdings(db, include_closed=False) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_investments_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bot.investments_store'`.

- [ ] **Step 3: Write `bot/investments_store.py`**

```python
"""DB-backed store for investment holdings, valuation rounds, and values.

Replaces the Google Sheet -> xlsx -> parse-on-read pipeline that
``bot/investments.py`` implements. A *round* is a labelled group of
observations, not a claim that every value shares one date: each
``holding_value`` carries its own ``as_of_date``.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from bot.storage import connect

_HOLDING_FIELDS = (
    "name", "owner", "kind", "account_type", "institution", "account_number",
    "tax_treatment", "ledger_account_id", "closed", "sort_order", "notes",
)

_VALUE_FIELDS = (
    "value_cents", "market_value_cents", "debt_cents", "vested_cents",
    "units", "unit_price_cents", "note",
)


def _new_id() -> str:
    return uuid.uuid4().hex


# ── Rounds ───────────────────────────────────────────────────────────────

def create_round(
    db_path: Path | str,
    *,
    label: str,
    as_of_date: str,
    seed_from_previous: bool = False,
) -> str:
    """Create a valuation round. Returns its id.

    With ``seed_from_previous`` the most recent earlier round's values are
    copied forward and flagged ``is_seeded=1``, so a new round starts as
    "confirm or change each number" rather than 23 blank fields. Seeded
    rows take the NEW round's date — carrying the old date forward is the
    fiction this whole design exists to remove.
    """
    rid = _new_id()
    with connect(db_path) as con:
        con.execute(
            "INSERT INTO snapshot_round (id, label, as_of_date) VALUES (?, ?, ?)",
            (rid, label, as_of_date),
        )
        if seed_from_previous:
            prev = con.execute(
                "SELECT id FROM snapshot_round WHERE as_of_date < ? "
                "ORDER BY as_of_date DESC LIMIT 1",
                (as_of_date,),
            ).fetchone()
            if prev is not None:
                con.execute(
                    """
                    INSERT INTO holding_value (
                        holding_id, round_id, as_of_date, value_cents,
                        market_value_cents, debt_cents, vested_cents,
                        units, unit_price_cents, source, is_seeded
                    )
                    SELECT hv.holding_id, ?, ?, hv.value_cents,
                           hv.market_value_cents, hv.debt_cents, hv.vested_cents,
                           hv.units, hv.unit_price_cents, 'manual', 1
                      FROM holding_value hv
                      JOIN holding h ON h.id = hv.holding_id
                     WHERE hv.round_id = ? AND h.closed = 0
                    """,
                    (rid, as_of_date, prev["id"]),
                )
    return rid


def list_rounds(db_path: Path | str) -> list[dict[str, Any]]:
    """Newest first."""
    with connect(db_path) as con:
        rows = con.execute(
            """
            SELECT r.id, r.label, r.as_of_date,
                   (SELECT COUNT(*) FROM holding_value hv
                     WHERE hv.round_id = r.id) AS value_count
              FROM snapshot_round r
             ORDER BY r.as_of_date DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def latest_round(db_path: Path | str) -> dict[str, Any] | None:
    rounds = list_rounds(db_path)
    return rounds[0] if rounds else None


# ── Holdings ─────────────────────────────────────────────────────────────

def upsert_holding(db_path: Path | str, *, id: str | None = None, **fields: Any) -> str:
    """Create (no id) or update (id given) a holding. Returns the id."""
    unknown = set(fields) - set(_HOLDING_FIELDS)
    if unknown:
        raise ValueError(f"unknown holding fields: {sorted(unknown)}")
    with connect(db_path) as con:
        if id is None:
            hid = _new_id()
            cols = ["id"] + list(fields)
            vals = [hid] + [fields[c] for c in fields]
            placeholders = ", ".join("?" for _ in cols)
            con.execute(
                f"INSERT INTO holding ({', '.join(cols)}) VALUES ({placeholders})",
                vals,
            )
            return hid
        if not fields:
            return id
        assignments = ", ".join(f"{c} = ?" for c in fields)
        con.execute(
            f"UPDATE holding SET {assignments} WHERE id = ?",
            [fields[c] for c in fields] + [id],
        )
        return id


def list_holdings(
    db_path: Path | str, *, include_closed: bool = True,
) -> list[dict[str, Any]]:
    where = "" if include_closed else "WHERE h.closed = 0"
    with connect(db_path) as con:
        rows = con.execute(
            f"""
            SELECT h.*, p.address, p.is_primary_residence, p.listed_price_cents,
                   p.escrow_cents, p.valuation_source
              FROM holding h
              LEFT JOIN property_detail p ON p.holding_id = h.id
              {where}
             ORDER BY h.closed, h.sort_order, h.name
            """
        ).fetchall()
    return [dict(r) for r in rows]


# ── Values ───────────────────────────────────────────────────────────────

def upsert_values(
    db_path: Path | str,
    *,
    round_id: str,
    values: list[dict[str, Any]],
    source: str = "manual",
) -> int:
    """Insert-or-update one round's values in a single transaction.

    Each entry needs ``holding_id`` and ``value_cents``; ``as_of_date``
    defaults to the round's date. Writing a value always clears
    ``is_seeded`` — a number that was confirmed is no longer carried
    forward.
    """
    with connect(db_path) as con:
        row = con.execute(
            "SELECT as_of_date FROM snapshot_round WHERE id = ?", (round_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"no such round: {round_id}")
        round_date = row["as_of_date"]
        round_date = round_date.isoformat() if hasattr(round_date, "isoformat") else round_date

        n = 0
        for v in values:
            unknown = set(v) - set(_VALUE_FIELDS) - {"holding_id", "as_of_date"}
            if unknown:
                raise ValueError(f"unknown value fields: {sorted(unknown)}")
            if "holding_id" not in v or v.get("value_cents") is None:
                raise ValueError("each value needs holding_id and value_cents")
            con.execute(
                """
                INSERT INTO holding_value (
                    holding_id, round_id, as_of_date, value_cents,
                    market_value_cents, debt_cents, vested_cents,
                    units, unit_price_cents, source, is_seeded, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                ON CONFLICT (holding_id, round_id) DO UPDATE SET
                    as_of_date         = excluded.as_of_date,
                    value_cents        = excluded.value_cents,
                    market_value_cents = excluded.market_value_cents,
                    debt_cents         = excluded.debt_cents,
                    vested_cents       = excluded.vested_cents,
                    units              = excluded.units,
                    unit_price_cents   = excluded.unit_price_cents,
                    source             = excluded.source,
                    is_seeded          = 0,
                    note               = excluded.note
                """,
                (
                    v["holding_id"], round_id, v.get("as_of_date") or round_date,
                    v["value_cents"], v.get("market_value_cents"), v.get("debt_cents"),
                    v.get("vested_cents"), v.get("units"), v.get("unit_price_cents"),
                    source, v.get("note"),
                ),
            )
            n += 1
    return n
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_investments_store.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add bot/investments_store.py tests/test_investments_store.py
git commit -m "feat(investments): rounds, holdings, and value upsert store"
```

---

## Task 3: Totals and snapshot assembly

**Files:**
- Modify: `bot/investments_store.py`
- Test: `tests/test_investments_store.py` (append)

**Interfaces:**
- Consumes: `create_round`, `upsert_holding`, `upsert_values`, `list_rounds`
- Produces:
  - `compute_totals(db_path) -> list[dict]` — `[{"label": str, "cells": list[int | str | None]}]`
  - `build_snapshot(db_path, *, round_id=None) -> dict` — the `InvestmentSnapshot` JSON shape

The totals rows, in order: **Total**, **Minus Home Equity**, **Annual Change**, **Target Savings**, **Delta**. One cell per round, oldest → newest, matching the `values` arrays.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_investments_store.py`:

```python
def _round_with(db, label, as_of, entries):
    rid = store.create_round(db, label=label, as_of_date=as_of)
    store.upsert_values(db, round_id=rid, values=entries)
    return rid


def test_minus_home_equity_subtracts_only_primary_residence(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    home = store.upsert_holding(db, name="117 Mayfield", kind="property")
    rental = store.upsert_holding(db, name="105 7th Ave", kind="property")
    cash = store.upsert_holding(db, name="Marcus", kind="cash")
    with connect(db) as con:
        con.execute(
            "INSERT INTO property_detail (holding_id, is_primary_residence) "
            "VALUES (?, 1)", (home,),
        )
        con.execute(
            "INSERT INTO property_detail (holding_id, is_primary_residence) "
            "VALUES (?, 0)", (rental,),
        )
    _round_with(db, "Jul 2026", "2026-07-25", [
        {"holding_id": home, "value_cents": 24952292},
        {"holding_id": rental, "value_cents": 6351669},
        {"holding_id": cash, "value_cents": 2566800},
    ])
    totals = {t["label"]: t["cells"] for t in store.compute_totals(db)}
    assert totals["Total"][0] == 24952292 + 6351669 + 2566800
    # rental stays in; only the primary residence comes out
    assert totals["Minus Home Equity"][0] == 6351669 + 2566800


def test_annual_change_annualizes_by_days(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    h = store.upsert_holding(db, name="Fund", kind="brokerage")
    _round_with(db, "A", "2025-07-25", [{"holding_id": h, "value_cents": 100000}])
    _round_with(db, "B", "2026-07-25", [{"holding_id": h, "value_cents": 200000}])
    totals = {t["label"]: t["cells"] for t in store.compute_totals(db)}
    assert totals["Annual Change"][0] is None          # no prior round
    assert totals["Annual Change"][1] == pytest.approx(100.0, abs=0.5)


def test_target_and_delta_use_savings_target_row(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    h = store.upsert_holding(db, name="Fund", kind="brokerage")
    _round_with(db, "Jul 2026", "2026-07-25", [
        {"holding_id": h, "value_cents": 100000000},
    ])
    with connect(db) as con:
        con.execute(
            "INSERT INTO savings_target "
            "(id, effective_year, age, combined_salary_cents, multiplier) "
            "VALUES ('t1', 2026, 42, 29800000, 3.0)"
        )
    totals = {t["label"]: t["cells"] for t in store.compute_totals(db)}
    assert totals["Target Savings"][0] == 89400000
    assert totals["Delta"][0] == 100000000 - 89400000


def test_target_steps_to_4x_at_45(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    h = store.upsert_holding(db, name="Fund", kind="brokerage")
    _round_with(db, "Jul 2029", "2029-07-25", [
        {"holding_id": h, "value_cents": 1},
    ])
    with connect(db) as con:
        con.execute(
            "INSERT INTO savings_target "
            "(id, effective_year, age, combined_salary_cents, multiplier) "
            "VALUES ('t1', 2026, 42, 29800000, 3.0)"
        )
        con.execute(
            "INSERT INTO savings_target "
            "(id, effective_year, age, combined_salary_cents, multiplier) "
            "VALUES ('t2', 2029, 45, 29800000, 4.0)"
        )
    totals = {t["label"]: t["cells"] for t in store.compute_totals(db)}
    assert totals["Target Savings"][0] == 119200000


def test_build_snapshot_matches_typescript_shape(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    home = store.upsert_holding(
        db, name="117 Mayfield", kind="property", account_type="Home Equity",
        owner="joint", account_number="", notes="Zestimate",
    )
    fund = store.upsert_holding(
        db, name="Roth IRA", kind="retirement", account_type="Roth IRA",
        owner="steven", account_number="1234",
    )
    _round_with(db, "Feb 2026", "2026-02-15", [
        {"holding_id": home, "value_cents": 100},
        {"holding_id": fund, "value_cents": 200},
    ])
    _round_with(db, "Jul 2026", "2026-07-25", [
        {"holding_id": home, "value_cents": 300},
        {"holding_id": fund, "value_cents": 400},
    ])
    snap = store.build_snapshot(db)
    assert set(snap) == {
        "source_file", "as_of", "holdings", "insurance", "totals_rows",
    }
    assert snap["as_of"] == "2026-07-25"
    assert snap["source_file"] == "database"
    by_name = {h["name"]: h for h in snap["holdings"]}
    assert set(by_name) == {"117 Mayfield", "Roth IRA"}
    assert by_name["117 Mayfield"]["is_real_estate"] is True
    assert by_name["Roth IRA"]["is_real_estate"] is False
    assert by_name["Roth IRA"]["account_type"] == "Roth IRA"
    # oldest -> newest, one cell per round, never compressed
    assert [v["cents"] for v in by_name["Roth IRA"]["values"]] == [200, 400]
    assert [v["snapshot_date"] for v in by_name["Roth IRA"]["values"]] == [
        "2026-02-15", "2026-07-25",
    ]
    assert [v["label"] for v in by_name["Roth IRA"]["values"]] == ["Feb 2026", "Jul 2026"]
    total = next(t for t in snap["totals_rows"] if t["label"] == "Total")
    assert total["cells"] == [300, 700]


def test_snapshot_emits_full_length_arrays_for_gap_holdings(tmp_path):
    """A holding with no value in an early round still gets a cell.

    The xlsx parser dropped empty cells, which compressed the arrays and
    made InvestmentsOverview.buildSeries() depend on finding a complete
    row. From the DB every array is round-aligned.
    """
    db = tmp_path / "t.db"
    init_db(db)
    old = store.upsert_holding(db, name="Old", kind="cash")
    new = store.upsert_holding(db, name="New", kind="cash")
    _round_with(db, "Feb 2026", "2026-02-15", [{"holding_id": old, "value_cents": 100}])
    _round_with(db, "Jul 2026", "2026-07-25", [
        {"holding_id": old, "value_cents": 150},
        {"holding_id": new, "value_cents": 900},
    ])
    snap = store.build_snapshot(db)
    by_name = {h["name"]: h for h in snap["holdings"]}
    assert len(by_name["New"]["values"]) == 2
    assert by_name["New"]["values"][0]["cents"] == 0
    assert len(by_name["Old"]["values"]) == 2


def test_build_snapshot_round_id_truncates_history(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    h = store.upsert_holding(db, name="Fund", kind="brokerage")
    r1 = _round_with(db, "Feb 2026", "2026-02-15", [
        {"holding_id": h, "value_cents": 100},
    ])
    _round_with(db, "Jul 2026", "2026-07-25", [{"holding_id": h, "value_cents": 200}])
    snap = store.build_snapshot(db, round_id=r1)
    assert snap["as_of"] == "2026-02-15"
    assert [v["cents"] for v in snap["holdings"][0]["values"]] == [100]
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_investments_store.py -v`
Expected: FAIL — `AttributeError: module 'bot.investments_store' has no attribute 'compute_totals'`.

- [ ] **Step 3: Add totals and snapshot assembly**

Append to `bot/investments_store.py`:

```python
from datetime import date as _date

# Totals are COMPUTED, never stored. The sheet stored them and they drifted
# out of sync with their own inputs — two columns carried wrong header
# dates and the Feb 2026 column was labelled 2025.


def _as_iso(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _round_cells(db_path: Path | str) -> tuple[list[dict[str, Any]], dict[str, dict[str, int]]]:
    """Return (rounds oldest-first, {round_id: {holding_id: value_cents}})."""
    rounds = list(reversed(list_rounds(db_path)))
    with connect(db_path) as con:
        rows = con.execute(
            "SELECT round_id, holding_id, value_cents FROM holding_value"
        ).fetchall()
    by_round: dict[str, dict[str, int]] = {r["id"]: {} for r in rounds}
    for row in rows:
        by_round.setdefault(row["round_id"], {})[row["holding_id"]] = row["value_cents"]
    return rounds, by_round


def compute_totals(db_path: Path | str) -> list[dict[str, Any]]:
    rounds, by_round = _round_cells(db_path)
    with connect(db_path) as con:
        primary = {
            r["holding_id"] for r in con.execute(
                "SELECT holding_id FROM property_detail WHERE is_primary_residence = 1"
            )
        }
        targets = [
            dict(r) for r in con.execute(
                "SELECT effective_year, combined_salary_cents, multiplier "
                "FROM savings_target ORDER BY effective_year"
            )
        ]

    total_cells: list[int] = []
    minus_home_cells: list[int] = []
    change_cells: list[float | None] = []
    target_cells: list[int | None] = []
    delta_cells: list[int | None] = []

    prev_total: int | None = None
    prev_date: _date | None = None

    for r in rounds:
        cells = by_round.get(r["id"], {})
        total = sum(cells.values())
        home = sum(v for hid, v in cells.items() if hid in primary)
        total_cells.append(total)
        minus_home_cells.append(total - home)

        as_of = _date.fromisoformat(_as_iso(r["as_of_date"]))
        # Both totals must be positive: a negative base raised to a
        # fractional exponent is a complex number, and round() rejects it.
        # A leveraged property makes a negative round total reachable.
        if prev_total is None or prev_total <= 0 or total <= 0 or prev_date is None:
            change_cells.append(None)
        else:
            days = (as_of - prev_date).days
            if days <= 0:
                change_cells.append(None)
            else:
                growth = total / prev_total
                change_cells.append(round((growth ** (365 / days) - 1) * 100, 2))
        prev_total, prev_date = total, as_of

        # Most recent target row at or before this round's year.
        applicable = [t for t in targets if t["effective_year"] <= as_of.year]
        if applicable:
            t = applicable[-1]
            target = int(round(t["combined_salary_cents"] * t["multiplier"]))
            target_cells.append(target)
            delta_cells.append(minus_home_cells[-1] - target)
        else:
            target_cells.append(None)
            delta_cells.append(None)

    return [
        {"label": "Total", "cells": total_cells},
        {"label": "Minus Home Equity", "cells": minus_home_cells},
        {"label": "Annual Change", "cells": change_cells},
        {"label": "Target Savings", "cells": target_cells},
        {"label": "Delta", "cells": delta_cells},
    ]


def build_snapshot(
    db_path: Path | str, *, round_id: str | None = None,
) -> dict[str, Any]:
    """Assemble the InvestmentSnapshot payload the Tauri pages consume.

    Shape is pinned by ``src/lib/types.ts:327-333`` — changing keys here
    breaks InvestmentsOverview/Holdings/Allocation/Insurance.
    """
    from bot import investments_insurance as ins

    rounds = list(reversed(list_rounds(db_path)))
    if round_id is not None:
        idx = next(
            (i for i, r in enumerate(rounds) if r["id"] == round_id), None,
        )
        if idx is None:
            raise ValueError(f"no such round: {round_id}")
        rounds = rounds[: idx + 1]
    if not rounds:
        return {
            "source_file": "database", "as_of": None,
            "holdings": [], "insurance": [], "totals_rows": [],
        }

    round_ids = [r["id"] for r in rounds]
    _, by_round = _round_cells(db_path)

    holdings_out: list[dict[str, Any]] = []
    for h in list_holdings(db_path, include_closed=True):
        values = [
            {
                "label": r["label"],
                "snapshot_date": _as_iso(r["as_of_date"]),
                "cents": by_round.get(r["id"], {}).get(h["id"], 0),
            }
            for r in rounds
        ]
        holdings_out.append({
            "name": h["name"],
            "account_type": h["account_type"] or h["kind"],
            "account_number": h["account_number"] or "",
            "owner": h["owner"] or "",
            "values": values,
            "notes": h["notes"] or "",
            "is_real_estate": h["kind"] == "property",
        })

    totals = [
        {"label": t["label"], "cells": t["cells"][: len(rounds)]}
        for t in compute_totals(db_path)
    ]

    return {
        "source_file": "database",
        "as_of": _as_iso(rounds[-1]["as_of_date"]),
        "holdings": holdings_out,
        "insurance": ins.list_policies_for_snapshot(db_path),
        "totals_rows": totals,
    }
```

- [ ] **Step 4: Create the insurance module stub so the import resolves**

Create `bot/investments_insurance.py` with just enough to satisfy the import; Task 4 fills it in:

```python
"""Insurance policy registry and premium reconciliation."""
from __future__ import annotations

from pathlib import Path
from typing import Any


def list_policies_for_snapshot(db_path: Path | str) -> list[dict[str, Any]]:
    return []
```

- [ ] **Step 5: Run to verify pass**

Run: `python -m pytest tests/test_investments_store.py -v`
Expected: PASS (14 tests).

- [ ] **Step 6: Commit**

```bash
git add bot/investments_store.py bot/investments_insurance.py tests/test_investments_store.py
git commit -m "feat(investments): computed totals and snapshot assembly"
```

---

## Task 4: Insurance registry, premiums, and drift

**Files:**
- Modify: `bot/investments_insurance.py`
- Test: `tests/test_investments_insurance.py`

**Interfaces:**
- Consumes: `storage.connect`
- Produces:
  - `upsert_policy(db_path, *, id=None, **fields) -> str`
  - `record_premium(db_path, *, policy_id, as_of_date, amount_cents, source="manual", note=None) -> int`
  - `annualize(premium_cents, frequency) -> int | None`
  - `list_policies(db_path, *, today=None) -> list[dict]` — registry + drift
  - `list_policies_for_snapshot(db_path) -> list[dict]` — the `InsurancePolicy` TS shape

- [ ] **Step 1: Write the failing tests**

Create `tests/test_investments_insurance.py`:

```python
from datetime import date

from bot.storage import init_db
from bot import investments_insurance as ins


def test_annualize_by_frequency():
    assert ins.annualize(10000, "monthly") == 120000
    assert ins.annualize(10000, "quarterly") == 40000
    assert ins.annualize(10000, "semiannual") == 20000
    assert ins.annualize(10000, "annual") == 10000
    assert ins.annualize(None, "annual") is None


def test_upsert_policy_creates_then_updates(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    pid = ins.upsert_policy(
        db, insurance_type="Homeowners", provider="Amica",
        premium_cents=210500, premium_frequency="annual", paid_via="escrow",
    )
    same = ins.upsert_policy(db, id=pid, premium_cents=279800)
    assert same == pid
    rows = ins.list_policies(db)
    assert len(rows) == 1
    assert rows[0]["premium_cents"] == 279800


def test_drift_uses_latest_observation(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    pid = ins.upsert_policy(
        db, insurance_type="Homeowners", provider="Amica",
        premium_cents=210500, premium_frequency="annual", paid_via="escrow",
    )
    ins.record_premium(
        db, policy_id=pid, as_of_date="2026-07-01",
        amount_cents=279800, source="escrow",
    )
    ins.record_premium(
        db, policy_id=pid, as_of_date="2025-07-01",
        amount_cents=210500, source="escrow",
    )
    row = ins.list_policies(db, today=date(2026, 7, 25))[0]
    assert row["observed_cents"] == 279800          # latest, not the older one
    assert row["observed_source"] == "escrow"
    assert row["drift_cents"] == 69300
    assert row["unverified"] is False


def test_policy_without_observation_is_unverified_not_zero_drift(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    ins.upsert_policy(
        db, insurance_type="Umbrella", provider="Amica", premium_cents=50000,
    )
    row = ins.list_policies(db, today=date(2026, 7, 25))[0]
    assert row["observed_cents"] is None
    assert row["drift_cents"] is None
    assert row["unverified"] is True


def test_observation_older_than_12_months_is_unverified(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    pid = ins.upsert_policy(db, insurance_type="Flood", premium_cents=79802)
    ins.record_premium(
        db, policy_id=pid, as_of_date="2025-01-01", amount_cents=79802,
    )
    row = ins.list_policies(db, today=date(2026, 7, 25))[0]
    assert row["unverified"] is True
    assert row["drift_cents"] == 0


def test_snapshot_shape_annualizes_premium(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    ins.upsert_policy(
        db, insurance_type="Auto", provider="Amica", premium_cents=10000,
        premium_frequency="monthly", coverage="100/300", deductible="500",
        sales_contact="Amica agent", renewal_date="2027-01-01",
        comments="bundled", through_employer=0,
    )
    rows = ins.list_policies_for_snapshot(db)
    assert set(rows[0]) == {
        "insurance_type", "through_employer", "provider", "sales_contact",
        "coverage", "deductible", "annual_premium_cents", "comments",
        "renewal_date",
    }
    assert rows[0]["annual_premium_cents"] == 120000
    assert rows[0]["through_employer"] is False


def test_inactive_policies_excluded_from_snapshot(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    pid = ins.upsert_policy(db, insurance_type="Old Term Life")
    ins.upsert_policy(db, id=pid, active=0)
    assert ins.list_policies_for_snapshot(db) == []
    assert len(ins.list_policies(db)) == 1      # registry still shows it
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_investments_insurance.py -v`
Expected: FAIL — `AttributeError: module 'bot.investments_insurance' has no attribute 'annualize'`.

- [ ] **Step 3: Implement the module**

Replace the contents of `bot/investments_insurance.py`:

```python
"""Insurance policy registry and premium reconciliation.

Premium drift is why this exists. The July 2026 review found
homeowner-adjacent premiums had risen 42% since February
($3,626.10 -> $5,137.68/yr) with nothing surfacing it.

Ledger matching alone cannot catch that: Amica home, Fortegra, and Neptune
are all paid out of escrow inside the mortgage payment and never appear as
ledger payees. ``insurance_premium_observed`` is the hand-entered path that
makes those visible, which is why a policy with no observation reads as
*unverified* rather than as zero drift.
"""
from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path
from typing import Any

from bot.storage import connect

_FREQ_MULTIPLIER = {
    "annual": 1, "semiannual": 2, "quarterly": 4, "monthly": 12,
}

_POLICY_FIELDS = (
    "insurance_type", "provider", "policy_number", "covers", "through_employer",
    "coverage", "deductible", "premium_cents", "premium_frequency", "paid_via",
    "ledger_payee_norm", "sales_contact", "renewal_date", "comments", "active",
    "sort_order",
)

STALE_AFTER_DAYS = 365


def annualize(premium_cents: int | None, frequency: str | None) -> int | None:
    if premium_cents is None:
        return None
    return premium_cents * _FREQ_MULTIPLIER.get(frequency or "annual", 1)


def upsert_policy(db_path: Path | str, *, id: str | None = None, **fields: Any) -> str:
    unknown = set(fields) - set(_POLICY_FIELDS)
    if unknown:
        raise ValueError(f"unknown policy fields: {sorted(unknown)}")
    with connect(db_path) as con:
        if id is None:
            pid = uuid.uuid4().hex
            cols = ["id"] + list(fields)
            vals = [pid] + [fields[c] for c in fields]
            con.execute(
                f"INSERT INTO insurance_policy ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' for _ in cols)})",
                vals,
            )
            return pid
        if not fields:
            return id
        assignments = ", ".join(f"{c} = ?" for c in fields)
        con.execute(
            f"UPDATE insurance_policy SET {assignments} WHERE id = ?",
            [fields[c] for c in fields] + [id],
        )
        return id


def record_premium(
    db_path: Path | str,
    *,
    policy_id: str,
    as_of_date: str,
    amount_cents: int,
    source: str = "manual",
    note: str | None = None,
) -> int:
    with connect(db_path) as con:
        cur = con.execute(
            "INSERT INTO insurance_premium_observed "
            "(policy_id, as_of_date, amount_cents, source, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (policy_id, as_of_date, amount_cents, source, note),
        )
        return int(cur.lastrowid)


def _iso(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def list_policies(
    db_path: Path | str, *, today: date | None = None,
) -> list[dict[str, Any]]:
    """Registry rows with their latest observation and computed drift.

    ``drift_cents`` compares like with like: both sides annualized.
    ``unverified`` is True when there is no observation at all, or the most
    recent one is older than a year.
    """
    today = today or date.today()
    with connect(db_path) as con:
        # created_at is a TIMESTAMP column, so PARSE_DECLTYPES hands back a
        # datetime — not JSON-serializable. This dict goes out through a
        # FastAPI route, so select the columns explicitly rather than *.
        policies = [dict(r) for r in con.execute(
            "SELECT id, insurance_type, provider, policy_number, covers, "
            "through_employer, coverage, deductible, premium_cents, "
            "premium_frequency, paid_via, ledger_payee_norm, sales_contact, "
            "renewal_date, comments, active, sort_order "
            "FROM insurance_policy ORDER BY active DESC, sort_order, insurance_type"
        )]
        observations = [dict(r) for r in con.execute(
            # `id` breaks same-day ties: without it, which of two
            # observations sharing a date wins is unspecified SQL behavior.
            "SELECT policy_id, as_of_date, amount_cents, source "
            "FROM insurance_premium_observed ORDER BY as_of_date, id"
        )]

    latest: dict[str, dict[str, Any]] = {}
    for o in observations:
        latest[o["policy_id"]] = o      # ordered ascending, so last wins

    out = []
    for p in policies:
        expected = annualize(p["premium_cents"], p["premium_frequency"])
        obs = latest.get(p["id"])
        if obs is None:
            observed = observed_date = observed_source = None
            unverified = True
        else:
            observed = obs["amount_cents"]
            observed_date = _iso(obs["as_of_date"])
            observed_source = obs["source"]
            age = (today - date.fromisoformat(observed_date)).days
            unverified = age > STALE_AFTER_DAYS
        drift = (
            observed - expected
            if observed is not None and expected is not None
            else None
        )
        out.append({
            **p,
            "annual_premium_cents": expected,
            "observed_cents": observed,
            "observed_date": observed_date,
            "observed_source": observed_source,
            "drift_cents": drift,
            "unverified": unverified,
        })
    return out


def list_policies_for_snapshot(db_path: Path | str) -> list[dict[str, Any]]:
    """Active policies in the InsurancePolicy shape from types.ts:315-325."""
    with connect(db_path) as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM insurance_policy WHERE active = 1 "
            "ORDER BY sort_order, insurance_type"
        )]
    return [
        {
            "insurance_type": r["insurance_type"],
            "through_employer": (
                None if r["through_employer"] is None
                else bool(r["through_employer"])
            ),
            "provider": r["provider"] or "",
            "sales_contact": r["sales_contact"] or "",
            "coverage": r["coverage"] or "",
            "deductible": r["deductible"] or "",
            "annual_premium_cents": annualize(
                r["premium_cents"], r["premium_frequency"],
            ),
            "comments": r["comments"] or "",
            "renewal_date": r["renewal_date"] or "",
        }
        for r in rows
    ]
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_investments_insurance.py tests/test_investments_store.py -v`
Expected: PASS (21 tests).

- [ ] **Step 5: Commit**

```bash
git add bot/investments_insurance.py tests/test_investments_insurance.py
git commit -m "feat(investments): insurance registry with premium drift detection"
```

---

## Task 5: xlsx importer

**Files:**
- Create: `bot/investments_import.py`
- Test: `tests/test_investments_import.py`

**Interfaces:**
- Consumes: `bot.investments.parse_snapshot`, `investments_store.*`, `investments_insurance.*`
- Produces: `import_xlsx(db_path, xlsx_path=None) -> dict` — `{"rounds": int, "holdings": int, "values": int, "policies": int, "skipped_rounds": [str]}`

Import is idempotent on round `as_of_date` and on holding `name`. An unparseable column header **raises** — this is a one-time supervised operation, and a silently invented date is precisely the failure this design removes.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_investments_import.py`:

```python
from datetime import date

import pytest
from openpyxl import Workbook

from bot.storage import init_db, connect
from bot import investments_import as imp
from bot import investments_store as store

# NOTE: sqlite3 runs with detect_types=PARSE_DECLTYPES and storage.py:18-21
# registers DATE converters, so DATE columns come back as datetime.date,
# NOT str. Assertions against raw rows must use date(...) objects; only
# build_snapshot's payload is normalized to ISO strings via _as_iso().


def _make_xlsx(path, *, header_dates=("02-15-26", "07-25-26")):
    wb = Workbook()
    ws = wb.active
    ws.append(["Account", "Type", "Number", "Owner",
               f"2026 Value ({header_dates[0]})",
               f"2026 Value ({header_dates[1]})", "Notes"])
    ws.append(["Marcus", "Savings", "1234", "Joint", "$32,571.29", "$25,668.00", ""])
    ws.append(["117 Mayfield Dr", "Home Equity", "", "Joint",
               "$246,501.00", "$249,522.92", "Zestimate"])
    ws.append([])
    ws.append(["Type of Insurance", "Through Employer", "Provider", "Contact",
               "Coverage", "Deductible", "Annual Premium", "Comments", "Renewal"])
    ws.append(["Homeowners", "No", "Amica", "agent", "482k", "$1,000",
               "$2,798.00", "escrow", "2027-01-01"])
    wb.save(path)
    return path


def test_import_creates_rounds_holdings_values(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    xlsx = _make_xlsx(tmp_path / "snap.xlsx")
    result = imp.import_xlsx(db, xlsx)
    assert result["rounds"] == 2
    assert result["holdings"] == 2
    assert result["values"] == 4
    rounds = store.list_rounds(db)
    assert [r["as_of_date"] for r in rounds] == [date(2026, 7, 25), date(2026, 2, 15)]


def test_import_is_idempotent(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    xlsx = _make_xlsx(tmp_path / "snap.xlsx")
    imp.import_xlsx(db, xlsx)
    second = imp.import_xlsx(db, xlsx)
    assert second["rounds"] == 0
    assert second["values"] == 0            # additive only: nothing rewritten
    assert second["skipped_values"] == 4
    assert len(second["skipped_rounds"]) == 2
    assert len(store.list_rounds(db)) == 2
    assert len(store.list_holdings(db)) == 2
    with connect(db) as con:
        n = con.execute("SELECT COUNT(*) FROM holding_value").fetchone()[0]
    assert n == 4


def test_reimport_does_not_revert_a_hand_edited_value(tmp_path):
    """The sheet is being retired; it must never overwrite operator input."""
    db = tmp_path / "t.db"
    init_db(db)
    xlsx = _make_xlsx(tmp_path / "snap.xlsx")
    imp.import_xlsx(db, xlsx)

    holdings = {h["name"]: h for h in store.list_holdings(db)}
    newest = store.list_rounds(db)[0]
    store.upsert_values(db, round_id=newest["id"], values=[
        {"holding_id": holdings["Marcus"]["id"], "value_cents": 9999900},
    ])

    imp.import_xlsx(db, xlsx)

    with connect(db) as con:
        row = con.execute(
            "SELECT value_cents, source FROM holding_value "
            "WHERE holding_id = ? AND round_id = ?",
            (holdings["Marcus"]["id"], newest["id"]),
        ).fetchone()
    assert row["value_cents"] == 9999900
    assert row["source"] == "manual"


def test_bare_year_header_raises_instead_of_inventing_jan_1(tmp_path):
    """parse_snapshot falls back to YYYY-01-01; the importer must refuse it."""
    db = tmp_path / "t.db"
    init_db(db)
    xlsx = tmp_path / "bare.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["Account", "Type", "Number", "Owner", "2026 Value", "Notes"])
    ws.append(["Marcus", "Savings", "1234", "Joint", "$1.00", ""])
    wb.save(xlsx)
    with pytest.raises(ValueError, match="no parseable date"):
        imp.import_xlsx(db, xlsx)


def test_roth_ira_is_classified_roth_not_pretax(tmp_path):
    """"ira" is a substring of "roth ira" — longest needle must win."""
    assert imp._classify("Roth IRA") == ("retirement", "roth")
    assert imp._classify("Simple IRA") == ("retirement", "pretax")
    assert imp._classify("IRA") == ("retirement", "pretax")


def test_property_rows_get_property_kind_and_detail(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    imp.import_xlsx(db, _make_xlsx(tmp_path / "snap.xlsx"))
    holdings = {h["name"]: h for h in store.list_holdings(db)}
    assert holdings["117 Mayfield Dr"]["kind"] == "property"
    assert holdings["117 Mayfield Dr"]["is_primary_residence"] == 1
    assert holdings["Marcus"]["kind"] != "property"


def test_import_creates_policy_and_baseline_observation(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    imp.import_xlsx(db, _make_xlsx(tmp_path / "snap.xlsx"))
    with connect(db) as con:
        policies = [dict(r) for r in con.execute("SELECT * FROM insurance_policy")]
        obs = [dict(r) for r in con.execute(
            "SELECT * FROM insurance_premium_observed"
        )]
    assert len(policies) == 1
    assert policies[0]["premium_cents"] == 279800
    assert len(obs) == 1
    assert obs[0]["amount_cents"] == 279800
    assert obs[0]["as_of_date"] == date(2026, 7, 25)   # newest round's date


def test_unparseable_header_raises(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    xlsx = tmp_path / "bad.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["Account", "Type", "Number", "Owner", "Value", "Notes"])
    ws.append(["Marcus", "Savings", "1234", "Joint", "$1.00", ""])
    wb.save(xlsx)
    with pytest.raises(ValueError, match="no parseable date"):
        imp.import_xlsx(db, xlsx)
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_investments_import.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bot.investments_import'`.

- [ ] **Step 3: Read the existing parser before writing the importer**

Read `bot/investments.py` in full. The importer reuses, rather than reimplements:
- `parse_snapshot(path)` → `{source_file, as_of, holdings, insurance, totals_rows}`
- `_extract_date_from_label(label)` → the date regexed out of `"2026 Value (02-15-26)"`
- `_REAL_ESTATE_TYPES` → the account-type strings that mean "property"

Note the parser **drops empty cells**, so a holding's `values` list is shorter than the number of columns when it has gaps. The importer must therefore key each value by its `snapshot_date`, not by list position.

- [ ] **Step 4: Write the importer**

Create `bot/investments_import.py`:

```python
"""One-time import: snapshot xlsx -> DB.

Reuses ``bot/investments.py`` as the parser rather than reimplementing it,
so the two paths can be diffed against each other at cutover
(``scripts/verify_investments_import.py``).

Idempotent on round ``as_of_date`` and holding ``name``: re-running adds
nothing. An unparseable column header raises — this runs once, under
supervision, and a silently invented date is the exact class of error this
design exists to remove.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from bot import investments as parser
from bot import investments_insurance as ins
from bot import investments_store as store
from bot.storage import connect

log = logging.getLogger(__name__)

# A column header must carry a real day/month/year, e.g. "(02-15-26)".
_FULL_DATE_RE = re.compile(r"\d{1,2}\s*[-/]\s*\d{1,2}\s*[-/]\s*\d{2,4}")

_KIND_BY_TYPE = {
    "401k": "retirement", "403b": "retirement", "ira": "retirement",
    "roth ira": "retirement", "roth": "retirement", "simple ira": "retirement",
    "pension": "retirement", "profit sharing": "retirement",
    "529": "education", "utma": "education",
    "hsa": "cash", "savings": "cash", "checking": "cash", "cd": "cash",
    "t-bills": "cash", "treasury": "cash",
    "crypto": "crypto", "bitcoin": "crypto", "ethereum": "crypto",
    "brokerage": "brokerage", "stock": "brokerage", "tod": "brokerage",
}

_TAX_BY_TYPE = {
    "roth ira": "roth", "roth": "roth", "401k": "pretax", "403b": "pretax",
    "ira": "pretax", "simple ira": "pretax", "hsa": "hsa", "529": "529",
}


def _classify(account_type: str) -> tuple[str, str | None]:
    """(kind, tax_treatment) from the sheet's free-text account type.

    Longest needle wins. Plain dict order would let "ira" match inside
    "roth ira" and tag a Roth as pretax — the kind of error that survives
    review because `kind` comes out "retirement" either way.
    """
    key = (account_type or "").strip().lower()
    if key in parser._REAL_ESTATE_TYPES:
        return "property", None
    for needle in sorted(_KIND_BY_TYPE, key=len, reverse=True):
        if needle in key:
            return _KIND_BY_TYPE[needle], _TAX_BY_TYPE.get(needle)
    return "other", None


def import_xlsx(
    db_path: Path | str, xlsx_path: Path | str | None = None,
) -> dict[str, Any]:
    path = Path(xlsx_path) if xlsx_path else parser.find_latest_snapshot()
    if path is None:
        raise ValueError(f"no xlsx files in {parser.SNAPSHOTS_DIR}")
    snap = parser.parse_snapshot(path)

    # ── Column dates. Every distinct snapshot_date across all holdings. ──
    #
    # The parser is NOT trusted to have found a real date. Its
    # _extract_date_from_label falls back to a bare 4-digit year and
    # returns a fabricated "YYYY-01-01", which is truthy — so checking
    # `snapshot_date is not None` would wave through exactly the invented
    # date this import is supposed to refuse. Re-verify the label itself
    # carries a full day/month/year.
    dates: dict[str, str] = {}      # iso date -> label
    for h in snap["holdings"]:
        for v in h["values"]:
            label = v.get("label") or ""
            if not v.get("snapshot_date") or not _FULL_DATE_RE.search(label):
                raise ValueError(
                    f"no parseable date in column label {label!r} — "
                    "fix the header before importing"
                )
            dates[v["snapshot_date"]] = label
    if not dates:
        raise ValueError("no parseable date columns found in the workbook")

    round_by_date: dict[str, str] = {}
    created_rounds = 0
    skipped: list[str] = []
    with connect(db_path) as con:
        for iso in sorted(dates):
            row = con.execute(
                "SELECT id FROM snapshot_round WHERE as_of_date = ?", (iso,),
            ).fetchone()
            if row is not None:
                round_by_date[iso] = row["id"]
                skipped.append(iso)
    for iso in sorted(dates):
        if iso not in round_by_date:
            round_by_date[iso] = store.create_round(
                db_path, label=dates[iso], as_of_date=iso,
            )
            created_rounds += 1

    # ── Holdings, keyed by name so re-import can't duplicate them ──
    #
    # Import is ADDITIVE ONLY. A holding or value that already exists is
    # left exactly as it is. Anything else means a re-run silently reverts
    # hand-corrections back to the sheet's numbers — the sheet is the thing
    # being retired, so it must never win over what the operator entered.
    with connect(db_path) as con:
        by_name = {
            r["name"]: r["id"] for r in con.execute("SELECT id, name FROM holding")
        }
        existing_values = {
            (r["holding_id"], r["round_id"]) for r in con.execute(
                "SELECT holding_id, round_id FROM holding_value"
            )
        }

    created_holdings = 0
    written_values = 0
    skipped_values = 0
    for order, h in enumerate(snap["holdings"]):
        kind, tax = _classify(h.get("account_type", ""))
        hid = by_name.get(h["name"])
        fields = dict(
            name=h["name"],
            owner=h.get("owner") or None,
            kind=kind,
            account_type=h.get("account_type") or None,
            account_number=h.get("account_number") or None,
            tax_treatment=tax,
            notes=h.get("notes") or None,
            sort_order=order,
        )
        if hid is None:
            hid = store.upsert_holding(db_path, **fields)
            by_name[h["name"]] = hid
            created_holdings += 1
        # else: holding already exists — leave its metadata alone.

        if kind == "property":
            with connect(db_path) as con:
                con.execute(
                    "INSERT OR IGNORE INTO property_detail "
                    "(holding_id, address, is_primary_residence) VALUES (?, ?, ?)",
                    (hid, h["name"], 1 if "mayfield" in h["name"].lower() else 0),
                )

        # Values are keyed by their own date — the parser compresses arrays
        # by dropping empty cells, so list position means nothing here.
        for v in h["values"]:
            rid = round_by_date[v["snapshot_date"]]
            if (hid, rid) in existing_values:
                skipped_values += 1
                continue
            store.upsert_values(
                db_path, round_id=rid, source="xlsx_import",
                values=[{
                    "holding_id": hid,
                    "value_cents": v["cents"],
                    "as_of_date": v["snapshot_date"],
                }],
            )
            existing_values.add((hid, rid))
            written_values += 1

    # ── Insurance: registry + one baseline observation at the newest date ──
    newest_iso = max(dates)
    with connect(db_path) as con:
        known = {
            r["insurance_type"] for r in con.execute(
                "SELECT insurance_type FROM insurance_policy"
            )
        }
    created_policies = 0
    for order, p in enumerate(snap["insurance"]):
        if p["insurance_type"] in known:
            continue
        pid = ins.upsert_policy(
            db_path,
            insurance_type=p["insurance_type"],
            provider=p.get("provider") or None,
            through_employer=(
                None if p.get("through_employer") is None
                else int(bool(p["through_employer"]))
            ),
            coverage=p.get("coverage") or None,
            deductible=p.get("deductible") or None,
            premium_cents=p.get("annual_premium_cents"),
            premium_frequency="annual",
            paid_via="ledger",
            sales_contact=p.get("sales_contact") or None,
            renewal_date=p.get("renewal_date") or None,
            comments=p.get("comments") or None,
            sort_order=order,
        )
        created_policies += 1
        if p.get("annual_premium_cents") is not None:
            ins.record_premium(
                db_path, policy_id=pid, as_of_date=newest_iso,
                amount_cents=p["annual_premium_cents"], source="manual",
                note=f"baseline imported from {path.name}",
            )

    # Counts report CREATIONS only, so a supervising operator can read
    # "0 / 0 / 0" as "this changed nothing" and trust it.
    result = {
        "source_file": str(path),
        "rounds": created_rounds,
        "holdings": created_holdings,
        "values": written_values,
        "skipped_values": skipped_values,
        "policies": created_policies,
        "skipped_rounds": skipped,
    }
    log.info("investments import: %s", result)
    return result
```

- [ ] **Step 5: Run to verify pass**

Run: `python -m pytest tests/test_investments_import.py -v`
Expected: PASS (5 tests). If `_classify` misses a type the fixture uses, extend `_KIND_BY_TYPE` — do not loosen the test.

- [ ] **Step 6: Commit**

```bash
git add bot/investments_import.py tests/test_investments_import.py
git commit -m "feat(investments): idempotent xlsx importer"
```

---

## Task 6: API routes

**Files:**
- Modify: `bot/http_api.py` — body models at module scope (after `YnabPushBody`, ~line 199); routes in the Investments block (~line 796)
- Test: `tests/test_investments_api.py` (create)

**Interfaces:**
- Consumes: `investments_store.*`, `investments_insurance.*`, `investments_import.import_xlsx`
- Produces: `GET /investments/snapshot` (now DB-backed, optional `?round_id=`), `GET /investments/rounds`, `GET /investments/holdings`, `GET /investments/insurance`, `POST /investments/round`, `POST /investments/values`, `POST /investments/holding`, `POST /investments/policy`, `POST /investments/policy/premium`, `POST /investments/import-xlsx`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_investments_api.py`:

```python
import json

import pytest
from fastapi.testclient import TestClient

from bot.http_api import build_app
from bot.storage import init_db
from bot import investments_store as store


@pytest.fixture()
def client(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    (token_dir / "ui_api_token.txt").write_text("testtoken", encoding="utf-8")
    app = build_app(db_path=db, token_dir=token_dir, webui_dir=tmp_path / "webui")
    c = TestClient(app)
    c.headers.update({"X-API-Token": "testtoken"})
    c.db = db
    return c


def test_snapshot_empty_db_returns_empty_shape(client):
    r = client.get("/investments/snapshot")
    assert r.status_code == 200
    body = r.json()
    assert body["holdings"] == []
    assert body["as_of"] is None


def test_round_and_values_roundtrip(client):
    hid = store.upsert_holding(client.db, name="Marcus", kind="cash")
    r = client.post("/investments/round", json={
        "label": "Jul 2026", "as_of_date": "2026-07-25",
    })
    assert r.status_code == 200
    rid = r.json()["round_id"]

    r = client.post("/investments/values", json={
        "round_id": rid,
        "values": [{"holding_id": hid, "value_cents": 2566800}],
    })
    assert r.status_code == 200
    assert r.json()["written"] == 1

    snap = client.get("/investments/snapshot").json()
    assert snap["as_of"] == "2026-07-25"
    assert snap["holdings"][0]["values"][0]["cents"] == 2566800


def test_values_rejects_unknown_round(client):
    r = client.post("/investments/values", json={
        "round_id": "nope", "values": [{"holding_id": "x", "value_cents": 1}],
    })
    assert r.status_code == 400


def test_holding_create_then_update(client):
    r = client.post("/investments/holding", json={
        "name": "Roth IRA", "kind": "retirement", "owner": "steven",
    })
    hid = r.json()["holding_id"]
    r = client.post("/investments/holding", json={"id": hid, "closed": True})
    assert r.json()["holding_id"] == hid
    rows = client.get("/investments/holdings").json()["holdings"]
    assert rows[0]["closed"] == 1


def test_policy_and_premium_routes(client):
    r = client.post("/investments/policy", json={
        "insurance_type": "Homeowners", "provider": "Amica",
        "premium_cents": 210500, "paid_via": "escrow",
    })
    pid = r.json()["policy_id"]
    r = client.post("/investments/policy/premium", json={
        "policy_id": pid, "as_of_date": "2026-07-01",
        "amount_cents": 279800, "source": "escrow",
    })
    assert r.status_code == 200
    rows = client.get("/investments/insurance").json()["policies"]
    assert rows[0]["drift_cents"] == 69300


def test_rounds_route_lists_newest_first(client):
    client.post("/investments/round", json={
        "label": "Feb 2026", "as_of_date": "2026-02-15"})
    client.post("/investments/round", json={
        "label": "Jul 2026", "as_of_date": "2026-07-25"})
    rounds = client.get("/investments/rounds").json()["rounds"]
    assert [r["label"] for r in rounds] == ["Jul 2026", "Feb 2026"]


def test_routes_require_token(client):
    r = client.get("/investments/snapshot", headers={"X-API-Token": "wrong"})
    assert r.status_code == 401


def test_mutations_write_audit_rows(client):
    client.post("/investments/round", json={
        "label": "Jul 2026", "as_of_date": "2026-07-25"})
    import sqlite3
    con = sqlite3.connect(client.db)
    events = {r[0] for r in con.execute("SELECT event FROM audit_log")}
    assert "ui_investments_round" in events
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_investments_api.py -v`
Expected: FAIL — `test_round_and_values_roundtrip` gets 404; `test_snapshot_empty_db_returns_empty_shape` gets 404 from the old xlsx route.

If the fixture errors on token loading, read `_load_token_map` in `http_api.py` and match the filename it expects.

- [ ] **Step 3: Add the body models at module scope**

In `bot/http_api.py`, after `class YnabPushBody` (~line 201):

```python
class InvestmentRoundBody(BaseModel):
    label: str
    as_of_date: str
    seed_from_previous: bool = False


class InvestmentValueItem(BaseModel):
    holding_id: str
    value_cents: int
    as_of_date: str | None = None
    market_value_cents: int | None = None
    debt_cents: int | None = None
    vested_cents: int | None = None
    units: float | None = None
    unit_price_cents: int | None = None
    note: str | None = None


class InvestmentValuesBody(BaseModel):
    round_id: str
    values: list[InvestmentValueItem]


class InvestmentHoldingBody(BaseModel):
    id: str | None = None
    name: str | None = None
    owner: str | None = None
    kind: str | None = None
    account_type: str | None = None
    institution: str | None = None
    account_number: str | None = None
    tax_treatment: str | None = None
    ledger_account_id: str | None = None
    closed: bool | None = None
    sort_order: int | None = None
    notes: str | None = None


class InsurancePolicyBody(BaseModel):
    id: str | None = None
    insurance_type: str | None = None
    provider: str | None = None
    policy_number: str | None = None
    covers: str | None = None
    through_employer: bool | None = None
    coverage: str | None = None
    deductible: str | None = None
    premium_cents: int | None = None
    premium_frequency: str | None = None
    paid_via: str | None = None
    ledger_payee_norm: str | None = None
    sales_contact: str | None = None
    renewal_date: str | None = None
    comments: str | None = None
    active: bool | None = None
    sort_order: int | None = None


class InsurancePremiumBody(BaseModel):
    policy_id: str
    as_of_date: str
    amount_cents: int
    source: str = "manual"
    note: str | None = None


class InvestmentImportBody(BaseModel):
    xlsx_path: str | None = None
```

- [ ] **Step 4: Replace the Investments route block**

In `bot/http_api.py`, replace the whole `# ── Investments ──` block (~lines 796-819). Keep `GET /investments/files` — it still lists importable xlsx files.

```python
    # ── Investments ───────────────────────────────────────────────────────

    @app.get("/investments/files", dependencies=[Depends(_require_token)])
    def investments_files() -> dict[str, Any]:
        """List importable snapshot xlsx files (newest first)."""
        from bot import investments as inv
        return {"folder": str(inv.SNAPSHOTS_DIR), "files": inv.list_snapshots()}

    @app.get("/investments/snapshot", dependencies=[Depends(_require_token)])
    def investments_snapshot(round_id: str | None = None) -> dict[str, Any]:
        """Snapshot assembled from the DB. Shape is pinned by types.ts."""
        from bot import investments_store as istore
        try:
            return istore.build_snapshot(db_path, round_id=round_id)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/investments/rounds", dependencies=[Depends(_require_token)])
    def investments_rounds() -> dict[str, Any]:
        from bot import investments_store as istore
        return {"rounds": istore.list_rounds(db_path)}

    @app.get("/investments/holdings", dependencies=[Depends(_require_token)])
    def investments_holdings(include_closed: bool = True) -> dict[str, Any]:
        from bot import investments_store as istore
        return {
            "holdings": istore.list_holdings(
                db_path, include_closed=include_closed,
            ),
        }

    @app.get("/investments/insurance", dependencies=[Depends(_require_token)])
    def investments_insurance_list() -> dict[str, Any]:
        from bot import investments_insurance as iins
        return {"policies": iins.list_policies(db_path)}

    @app.post("/investments/round", dependencies=[Depends(_require_token)])
    def investments_create_round(body: InvestmentRoundBody) -> dict[str, Any]:
        from bot import investments_store as istore
        try:
            rid = istore.create_round(
                db_path,
                label=body.label,
                as_of_date=body.as_of_date,
                seed_from_previous=body.seed_from_previous,
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:  # noqa: BLE001
            # A duplicate as_of_date is the operator's problem; anything
            # else is ours, and reporting it as 400 with no log would hide it.
            log.exception("create_round failed: %s", e)
            raise HTTPException(500, f"could not create round: {e}")
        storage.audit(db_path, "ui_investments_round", {
            "round_id": rid, "label": body.label, "as_of_date": body.as_of_date,
            "seeded": body.seed_from_previous,
        })
        return {"ok": True, "round_id": rid}

    @app.post("/investments/values", dependencies=[Depends(_require_token)])
    def investments_save_values(body: InvestmentValuesBody) -> dict[str, Any]:
        from bot import investments_store as istore
        payload = [v.model_dump(exclude_none=True) for v in body.values]
        try:
            n = istore.upsert_values(
                db_path, round_id=body.round_id, values=payload,
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        storage.audit(db_path, "ui_investments_values", {
            "round_id": body.round_id, "count": n,
        })
        return {"ok": True, "written": n}

    @app.post("/investments/holding", dependencies=[Depends(_require_token)])
    def investments_save_holding(body: InvestmentHoldingBody) -> dict[str, Any]:
        from bot import investments_store as istore
        fields = body.model_dump(exclude_none=True)
        hid = fields.pop("id", None)
        if "closed" in fields:
            fields["closed"] = int(fields["closed"])
        if hid is None and not fields.get("name"):
            raise HTTPException(400, "name required to create a holding")
        try:
            hid = istore.upsert_holding(db_path, id=hid, **fields)
        except ValueError as e:
            raise HTTPException(400, str(e))
        storage.audit(db_path, "ui_investments_holding", {
            "holding_id": hid, "fields": sorted(fields),
        })
        return {"ok": True, "holding_id": hid}

    @app.post("/investments/policy", dependencies=[Depends(_require_token)])
    def investments_save_policy(body: InsurancePolicyBody) -> dict[str, Any]:
        from bot import investments_insurance as iins
        fields = body.model_dump(exclude_none=True)
        pid = fields.pop("id", None)
        for flag in ("active", "through_employer"):
            if flag in fields:
                fields[flag] = int(fields[flag])
        if pid is None and not fields.get("insurance_type"):
            raise HTTPException(400, "insurance_type required to create a policy")
        try:
            pid = iins.upsert_policy(db_path, id=pid, **fields)
        except ValueError as e:
            raise HTTPException(400, str(e))
        storage.audit(db_path, "ui_investments_policy", {
            "policy_id": pid, "fields": sorted(fields),
        })
        return {"ok": True, "policy_id": pid}

    @app.post("/investments/policy/premium", dependencies=[Depends(_require_token)])
    def investments_record_premium(body: InsurancePremiumBody) -> dict[str, Any]:
        from bot import investments_insurance as iins
        try:
            obs_id = iins.record_premium(
                db_path,
                policy_id=body.policy_id,
                as_of_date=body.as_of_date,
                amount_cents=body.amount_cents,
                source=body.source,
                note=body.note,
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:  # noqa: BLE001
            log.exception("record_premium failed: %s", e)
            raise HTTPException(500, f"could not record premium: {e}")
        storage.audit(db_path, "ui_investments_premium", {
            "policy_id": body.policy_id, "amount_cents": body.amount_cents,
            "source": body.source,
        })
        return {"ok": True, "observation_id": obs_id}

    @app.post("/investments/import-xlsx", dependencies=[Depends(_require_token)])
    def investments_import(body: InvestmentImportBody) -> dict[str, Any]:
        """One-time migration. Idempotent on round date and holding name."""
        from bot import investments_import as iimp
        try:
            result = iimp.import_xlsx(db_path, body.xlsx_path)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:  # noqa: BLE001
            log.exception("investments import failed: %s", e)
            raise HTTPException(500, f"import failed: {e}")
        storage.audit(db_path, "ui_investments_import", result)
        return {"ok": True, **result}
```

- [ ] **Step 5: Run to verify pass**

Run: `python -m pytest tests/test_investments_api.py -v`
Expected: PASS (8 tests).

- [ ] **Step 6: Run the whole suite**

Run: `python -m pytest tests/ -q`
Expected: all pass. `test_webui_queries.py` and `test_telegram_bot.py` must be unaffected.

- [ ] **Step 7: Commit**

```bash
git add bot/http_api.py tests/test_investments_api.py
git commit -m "feat(investments): DB-backed snapshot route plus write endpoints"
```

---

## Task 7: Import verification script and cutover

**Files:**
- Create: `scripts/verify_investments_import.py`

**Interfaces:**
- Consumes: `bot.investments.parse_snapshot`, `bot.investments_store.build_snapshot`
- Produces: a CLI that exits 0 on zero diffs, 1 otherwise

This is the cutover gate. The xlsx parser and the DB store are independent implementations of the same numbers, which is what makes the diff meaningful.

- [ ] **Step 1: Write the script**

Create `scripts/verify_investments_import.py`:

```python
"""Diff the xlsx snapshot against the DB snapshot. Cutover gate.

Usage:
    python scripts/verify_investments_import.py [--xlsx PATH] [--db PATH]

Exits 0 when every holding, every value cell, and every totals row match.
Any difference is printed and exits 1.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import investments as parser          # noqa: E402
from bot import investments_store as store     # noqa: E402

DEFAULT_DB = Path("C:/Users/Steven/ynabhelper/ynab_helper.db")


def _cells_by_date(holding: dict) -> dict[str, int]:
    return {v["snapshot_date"]: v["cents"] for v in holding["values"]}


def _undated(holding: dict) -> bool:
    """True if any column header failed to yield a date.

    Two undated columns collapse into one dict key, silently hiding a whole
    column from the diff. The gate must refuse to certify that.
    """
    return any(not v.get("snapshot_date") for v in holding["values"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx", type=Path, default=None)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = ap.parse_args()

    xlsx = args.xlsx or parser.find_latest_snapshot()
    if xlsx is None:
        print(f"FAIL: no xlsx found in {parser.SNAPSHOTS_DIR}")
        return 1

    sheet = parser.parse_snapshot(xlsx)
    db = store.build_snapshot(args.db)
    problems: list[str] = []

    sheet_h = {h["name"]: h for h in sheet["holdings"]}
    db_h = {h["name"]: h for h in db["holdings"]}

    # 0. An undated column can't be diffed at all — refuse to certify.
    for name in sorted(sheet_h):
        if _undated(sheet_h[name]):
            problems.append(f"{name}: xlsx has a value column with no parseable date")

    # 1. Holding names, both directions.
    for name in sorted(set(sheet_h) - set(db_h)):
        problems.append(f"missing from DB: {name}")
    for name in sorted(set(db_h) - set(sheet_h)):
        problems.append(f"extra in DB (not in xlsx): {name}")

    # 2. Value cells, BOTH directions, keyed by date. The DB zero-fills every
    #    holding for every round while the xlsx omits blanks, so an absent
    #    xlsx cell must read 0 in the DB — anything else is data the import
    #    invented, and iterating only the xlsx side would never see it.
    for name in sorted(set(sheet_h) & set(db_h)):
        want = _cells_by_date(sheet_h[name])
        got = _cells_by_date(db_h[name])
        for iso in sorted(set(want) | set(got)):
            if want.get(iso, 0) != got.get(iso, 0):
                problems.append(
                    f"{name} @ {iso}: xlsx {want.get(iso, 0)} != db {got.get(iso, 0)}"
                )

    # 3. Totals, anchored to dates rather than list position.
    #    The xlsx's own totals row is NOT trusted as the comparand: the
    #    parser drops blank cells, so its cells can't be aligned to rounds,
    #    and this sheet has shipped wrong header dates before. Recompute the
    #    expected total from the xlsx holdings instead — that is a genuine
    #    cross-implementation check rather than two lists that happen to be
    #    the same length.
    db_dates = (
        [v["snapshot_date"] for v in db["holdings"][0]["values"]]
        if db["holdings"] else []
    )
    db_t = {t["label"]: t["cells"] for t in db["totals_rows"]}
    total_cells = db_t.get("Total", [])
    minus_home = db_t.get("Minus Home Equity", [])

    if len(total_cells) != len(db_dates):
        problems.append(
            f"DB Total has {len(total_cells)} cells for {len(db_dates)} rounds"
        )
    else:
        for i, iso in enumerate(db_dates):
            expected = sum(_cells_by_date(h).get(iso, 0) for h in sheet["holdings"])
            if expected != total_cells[i]:
                problems.append(
                    f"Total @ {iso}: xlsx sum {expected} != db {total_cells[i]}"
                )

    # 4. Minus Home Equity must subtract exactly one real-estate holding's
    #    value — the primary residence. Subtracting both properties, or the
    #    rental instead, is a ~$63,000 error that looks entirely plausible
    #    on screen. This is the arithmetic that was misread once already.
    #    The snapshot payload doesn't carry primary-residence identity, only
    #    raw values — so ask the store directly. Matching "some real-estate
    #    value" is not enough: subtracting the RENTAL instead of the home
    #    lands inside that set and passes. Only an exact match against the
    #    flagged primary residence closes the $63,000 error.
    primary_names = [
        h["name"] for h in store.list_holdings(args.db)
        if h.get("is_primary_residence")
    ]
    if len(primary_names) != 1:
        problems.append(
            f"expected exactly 1 primary residence, found {len(primary_names)}: "
            f"{sorted(primary_names)}"
        )
    elif len(minus_home) == len(total_cells) == len(db_dates):
        primary = primary_names[0]
        primary_cells = _cells_by_date(db_h[primary]) if primary in db_h else {}
        for i, iso in enumerate(db_dates):
            subtracted = total_cells[i] - minus_home[i]
            expected = primary_cells.get(iso, 0)
            if subtracted != expected:
                problems.append(
                    f"Minus Home Equity @ {iso}: subtracted {subtracted}, but the "
                    f"primary residence ({primary}) is {expected}"
                )

    # 5. Insurance: types AND premiums, not just a row count. Premium drift
    #    is the defect this whole system exists to surface — a gate blind to
    #    it would certify the one thing that must not slip through.
    sheet_ins = {p["insurance_type"]: p for p in sheet["insurance"]}
    db_ins = {p["insurance_type"]: p for p in db["insurance"]}
    if len(sheet_ins) != len(sheet["insurance"]):
        problems.append(
            "xlsx has duplicate insurance types; premiums can't be matched by type"
        )
    for t in sorted(set(sheet_ins) - set(db_ins)):
        problems.append(f"insurance missing from DB: {t}")
    for t in sorted(set(db_ins) - set(sheet_ins)):
        problems.append(f"insurance extra in DB: {t}")
    for t in sorted(set(sheet_ins) & set(db_ins)):
        w = sheet_ins[t].get("annual_premium_cents")
        g = db_ins[t].get("annual_premium_cents")
        if w != g:
            problems.append(f"insurance {t}: xlsx premium {w} != db {g}")
    n_db_ins = len(db["insurance"])

    if problems:
        print(f"FAIL — {len(problems)} difference(s):")
        for p in problems:
            print(f"  - {p}")
        return 1

    print(
        f"OK — {len(db_h)} holdings, "
        f"{len(db_t.get('Total', []))} rounds, {n_db_ins} policies match."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Back up the live DB before touching it**

```bash
cp "C:/Users/Steven/ynabhelper/ynab_helper.db" \
   "C:/Users/Steven/ynabhelper/ynab_helper.db.pre-investments-$(date +%Y%m%d)"
```

- [ ] **Step 3: Run the import against the live DB**

```bash
python -c "from bot.storage import init_db; from bot import investments_import as i; \
db='C:/Users/Steven/ynabhelper/ynab_helper.db'; init_db(db); print(i.import_xlsx(db))"
```

Expected: `{'rounds': 8, 'holdings': 32, 'values': ..., 'policies': 28, ...}`.
The deployed file is `G:\My Drive\ynabclone\investments\snapshot-2026-07-25.xlsx` — 32 holdings (23 active + 9 closed), 28 insurance rows, 8 date columns.

- [ ] **Step 4: Run the verifier**

Run: `python scripts/verify_investments_import.py`
Expected: `OK — 32 holdings, 8 rounds, 28 policies match.`

Do not proceed while any diff remains. `Minus Home Equity` disagreeing means `property_detail.is_primary_residence` was set on the wrong row — 117 Mayfield is the only 1.

- [ ] **Step 5: Seed the savings target and mark closed holdings**

```bash
python - <<'PY'
import sqlite3, uuid
db = "C:/Users/Steven/ynabhelper/ynab_helper.db"
con = sqlite3.connect(db)
con.execute(
    "INSERT OR REPLACE INTO savings_target "
    "(id, effective_year, age, combined_salary_cents, multiplier, note) "
    "VALUES (?, 2026, 42, 29800000, 3.0, 'Fidelity 3x at 40; $150k + $148k')",
    (uuid.uuid4().hex,),
)
con.execute(
    "INSERT OR REPLACE INTO savings_target "
    "(id, effective_year, age, combined_salary_cents, multiplier, note) "
    "VALUES (?, 2029, 45, 29800000, 4.0, 'Fidelity benchmark steps to 4x at 45')",
    (uuid.uuid4().hex,),
)
# Holdings whose newest value is 0 are the sheet's closed rows.
con.execute("""
    UPDATE holding SET closed = 1 WHERE id IN (
        SELECT hv.holding_id FROM holding_value hv
          JOIN snapshot_round r ON r.id = hv.round_id
         WHERE r.as_of_date = (SELECT MAX(as_of_date) FROM snapshot_round)
           AND hv.value_cents = 0
    )
""")
con.commit()
print("closed:", con.execute(
    "SELECT COUNT(*) FROM holding WHERE closed = 1").fetchone()[0])
PY
```

Expected: `closed: 9`.

- [ ] **Step 6: Re-run the verifier**

Run: `python scripts/verify_investments_import.py`
Expected: still OK. Closing a holding must not change any historical value.

- [ ] **Step 7: Commit**

```bash
git add scripts/verify_investments_import.py
git commit -m "feat(investments): xlsx-vs-db verification script"
```

---

## Task 8: UI types and API client

**Files:**
- Modify: `ynabhelper-ui/src/lib/types.ts` (after line 333), `ynabhelper-ui/src/lib/api.ts` (after line 224)

**Interfaces:**
- Consumes: the routes from Task 6
- Produces: `InvestmentRound`, `HoldingRow`, `PolicyRow` types; `getInvestmentRounds`, `getInvestmentHoldings`, `getInsurancePolicies`, `createInvestmentRound`, `saveInvestmentValues`, `saveHolding`, `savePolicy`, `recordPremium`

- [ ] **Step 1: Add the types**

Append to `ynabhelper-ui/src/lib/types.ts`:

```ts
// ─── Investments editing (DB-backed) ───────────────────────────────────────

export interface InvestmentRound {
  id: string;
  label: string;
  as_of_date: string;
  value_count: number;
}

export interface HoldingRow {
  id: string;
  name: string;
  owner: string | null;
  kind: "retirement" | "brokerage" | "crypto" | "cash" | "education" | "property" | "other";
  account_type: string | null;
  institution: string | null;
  account_number: string | null;
  tax_treatment: string | null;
  ledger_account_id: string | null;
  closed: number;
  sort_order: number;
  notes: string | null;
  address: string | null;
  is_primary_residence: number | null;
}

export interface HoldingValueInput {
  holding_id: string;
  value_cents: number;
  as_of_date?: string;
  market_value_cents?: number;
  debt_cents?: number;
  vested_cents?: number;
  units?: number;
  unit_price_cents?: number;
  note?: string;
}

export interface PolicyRow {
  id: string;
  insurance_type: string;
  provider: string | null;
  policy_number: string | null;
  covers: string | null;
  through_employer: number | null;
  coverage: string | null;
  deductible: string | null;
  premium_cents: number | null;
  premium_frequency: "annual" | "semiannual" | "quarterly" | "monthly";
  paid_via: "escrow" | "ledger" | "payroll";
  ledger_payee_norm: string | null;
  sales_contact: string | null;
  renewal_date: string | null;
  comments: string | null;
  active: number;
  annual_premium_cents: number | null;
  observed_cents: number | null;
  observed_date: string | null;
  observed_source: string | null;
  drift_cents: number | null;
  unverified: boolean;
}
```

- [ ] **Step 2: Add the client functions**

Append to `ynabhelper-ui/src/lib/api.ts`:

```ts
import type {
  InvestmentRound, HoldingRow, HoldingValueInput, PolicyRow,
} from "./types";

export function getInvestmentRounds(): Promise<{ rounds: InvestmentRound[] }> {
  return apiGet("/investments/rounds");
}

export function getInvestmentHoldings(): Promise<{ holdings: HoldingRow[] }> {
  return apiGet("/investments/holdings");
}

export function getInsurancePolicies(): Promise<{ policies: PolicyRow[] }> {
  return apiGet("/investments/insurance");
}

export function createInvestmentRound(opts: {
  label: string;
  as_of_date: string;
  seed_from_previous: boolean;
}): Promise<{ ok: boolean; round_id: string }> {
  return apiPost("/investments/round", opts);
}

/** Saves a whole round in one atomic request — not one call per row. */
export function saveInvestmentValues(opts: {
  round_id: string;
  values: HoldingValueInput[];
}): Promise<{ ok: boolean; written: number }> {
  return apiPost("/investments/values", opts);
}

export function saveHolding(
  opts: Partial<HoldingRow> & { id?: string },
): Promise<{ ok: boolean; holding_id: string }> {
  return apiPost("/investments/holding", opts);
}

export function savePolicy(
  opts: Partial<PolicyRow> & { id?: string },
): Promise<{ ok: boolean; policy_id: string }> {
  return apiPost("/investments/policy", opts);
}

export function recordPremium(opts: {
  policy_id: string;
  as_of_date: string;
  amount_cents: number;
  source: "escrow" | "ledger" | "manual";
  note?: string;
}): Promise<{ ok: boolean; observation_id: number }> {
  return apiPost("/investments/policy/premium", opts);
}
```

- [ ] **Step 3: Typecheck**

Run from `C:\Users\Steven\ynabhelper-ui`: `npx tsc --noEmit`
Expected: no errors. If `apiGet` is not exported at that point in the file, move the new functions below its definition rather than exporting it.

- [ ] **Step 4: Commit (in the UI repo)**

```bash
git add src/lib/types.ts src/lib/api.ts
git commit -m "feat(investments): types and API client for DB-backed editing"
```

---

## Task 9: Update Values screen

**Files:**
- Create: `ynabhelper-ui/src/pages/InvestmentsUpdate.tsx`
- Modify: `ynabhelper-ui/src/App.tsx`

**Interfaces:**
- Consumes: `getInvestmentRounds`, `getInvestmentHoldings`, `getInvestmentSnapshot`, `createInvestmentRound`, `saveInvestmentValues`, `saveHolding`
- Produces: route `/investments/update`

- [ ] **Step 1: Read two existing pages for the house style**

Read `src/pages/InvestmentsHoldings.tsx` (the `PageShell` + `useQuery` + table pattern) and `src/pages/SyncToYnab.tsx` (`useMutation` + `invalidateQueries` + per-row busy state).

House idioms to match: `fmtMoney(cents)` from `@/lib/format` (**not** `formatCents`), `cn()` from `@/lib/cn`, `<Skeleton className="h-96 w-full" />` while loading, and the Tailwind vocabulary `card`, `hairline`, `hairline-t`, `text-ink-2`, `text-ink-3`, `text-positive`, `text-negative`, `num`, `bg-surface-2`.

- [ ] **Step 2: Write the page skeleton**

Create `src/pages/InvestmentsUpdate.tsx`. This skeleton establishes state shape, query keys, and the save path; Step 3 fills in the per-kind row bodies.

```tsx
import { useEffect, useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import PageShell from "@/components/PageShell";
import { Skeleton } from "@/components/Skeleton";
import {
  getInvestmentRounds, getInvestmentHoldings, getInvestmentSnapshot,
  createInvestmentRound, saveInvestmentValues,
} from "@/lib/api";
import { fmtMoney } from "@/lib/format";
import { cn } from "@/lib/cn";
import type { HoldingRow, HoldingValueInput } from "@/lib/types";

/** One row's editable state. Dollars as strings so a half-typed "12." is
 *  not clobbered by a premature parse; converted to cents only on save. */
interface RowDraft {
  value: string;
  marketValue: string;
  debt: string;
  units: string;
  unitPrice: string;
  vested: string;
  asOf: string;
  touched: boolean;
}

const toCents = (s: string): number =>
  Math.round(parseFloat(s.replace(/[$,\s]/g, "")) * 100) || 0;

const fromCents = (c: number | null | undefined): string =>
  c == null ? "" : (c / 100).toFixed(2);

export default function InvestmentsUpdate() {
  const qc = useQueryClient();
  const [roundId, setRoundId] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, RowDraft>>({});

  const { data: roundsData } = useQuery({
    queryKey: ["investments_rounds"],
    queryFn: getInvestmentRounds,
  });
  const { data: holdingsData, isLoading } = useQuery({
    queryKey: ["investments_holdings"],
    queryFn: getInvestmentHoldings,
  });
  const rounds = roundsData?.rounds ?? [];
  const active = rounds.find((r) => r.id === roundId) ?? rounds[0] ?? null;

  useEffect(() => {
    if (!roundId && rounds.length > 0) setRoundId(rounds[0].id);
  }, [rounds, roundId]);

  // Scoped to the selected round: its values are the last cell, the prior
  // round's are the second-to-last.
  const { data: snap } = useQuery({
    queryKey: ["investments_snapshot", active?.id ?? "latest"],
    queryFn: () => getInvestmentSnapshot(active?.id),
    enabled: !!active,
  });

  const holdings = useMemo(
    () => (holdingsData?.holdings ?? []).filter((h) => h.closed === 0),
    [holdingsData],
  );

  /** The snapshot is name-keyed; the registry is id-keyed. */
  const { current, previous } = useMemo(() => {
    const cur: Record<string, number> = {};
    const prev: Record<string, number> = {};
    for (const h of snap?.holdings ?? []) {
      const v = h.values;
      cur[h.name] = v.length >= 1 ? v[v.length - 1].cents : 0;
      prev[h.name] = v.length >= 2 ? v[v.length - 2].cents : 0;
    }
    return { current: cur, previous: prev };
  }, [snap]);

  /** Seed drafts from what the round already holds — including values
   *  carried forward by seed_from_previous. Without this, saving an
   *  untouched round would write zeros over every row. */
  useEffect(() => {
    if (!active || !snap || holdings.length === 0) return;
    setDrafts((existing) => {
      if (Object.keys(existing).length > 0) return existing;   // don't stomp edits
      const seeded: Record<string, RowDraft> = {};
      for (const h of holdings) {
        seeded[h.id] = {
          ...blankDraft(active.as_of_date),
          value: fromCents(current[h.name] ?? 0),
        };
      }
      return seeded;
    });
  }, [active, snap, holdings, current]);

  const setField = (id: string, field: keyof RowDraft, v: string) =>
    setDrafts((d) => ({
      ...d,
      [id]: { ...blankDraft(active?.as_of_date), ...d[id], [field]: v, touched: true },
    }));

  const runningTotal = useMemo(
    () => holdings.reduce((sum, h) => sum + rowCents(h, drafts[h.id]), 0),
    [holdings, drafts],
  );

  const saveMut = useMutation({
    mutationFn: () => {
      if (!active) throw new Error("no round selected");
      const values: HoldingValueInput[] = holdings.map((h) => {
        const d = drafts[h.id];
        const base: HoldingValueInput = {
          holding_id: h.id,
          value_cents: rowCents(h, d),
          as_of_date: d?.asOf || active.as_of_date,
        };
        if (h.kind === "property" && (d?.marketValue || d?.debt)) {
          base.market_value_cents = toCents(d.marketValue);
          base.debt_cents = toCents(d.debt);
        }
        if (h.kind === "crypto" && (d?.units || d?.unitPrice)) {
          base.units = parseFloat(d.units) || 0;
          base.unit_price_cents = toCents(d.unitPrice);
        }
        if (d?.vested) base.vested_cents = toCents(d.vested);
        return base;
      });
      return saveInvestmentValues({ round_id: active.id, values });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["investments_snapshot"] });
      qc.invalidateQueries({ queryKey: ["investments_rounds"] });
      qc.invalidateQueries({ queryKey: ["investments_holdings"] });
      setDrafts({});
    },
  });

  const newRoundMut = useMutation({
    mutationFn: createInvestmentRound,
    onSuccess: (r) => {
      setRoundId(r.round_id);
      setDrafts({});
      qc.invalidateQueries({ queryKey: ["investments_rounds"] });
      qc.invalidateQueries({ queryKey: ["investments_snapshot"] });
    },
  });

  if (isLoading) {
    return (
      <PageShell title="Update Values" subtitle="Record this round's balances.">
        <Skeleton className="h-96 w-full" />
      </PageShell>
    );
  }

  return (
    <PageShell
      title="Update Values"
      subtitle={active ? `Round of ${active.as_of_date}` : "No rounds yet."}
    >
      {/* Header: round picker, New round, running total */}
      {/* Body: grouped rows — see Step 3 */}
      {/* Footer: Save round -> saveMut.mutate() */}
    </PageShell>
  );
}

function blankDraft(asOf?: string): RowDraft {
  return {
    value: "", marketValue: "", debt: "", units: "", unitPrice: "",
    vested: "", asOf: asOf ?? "", touched: false,
  };
}

/** Equity for property, units x price for crypto, the amount otherwise.
 *  Falls back to the plain amount when components are blank — historical
 *  rounds only ever stored the net number. */
function rowCents(h: HoldingRow, d?: RowDraft): number {
  if (!d) return 0;
  if (h.kind === "property" && (d.marketValue || d.debt)) {
    return toCents(d.marketValue) - toCents(d.debt);
  }
  if (h.kind === "crypto" && (d.units || d.unitPrice)) {
    return Math.round((parseFloat(d.units) || 0) * toCents(d.unitPrice));
  }
  return toCents(d.value);
}
```

- [ ] **Step 3: Fill in the header, rows, and footer**

**Header**
- Round `<select>` over `getInvestmentRounds()`, defaulting to the newest, plus a **New round** button opening a small inline form (`label`, `as_of_date` defaulting to today, `seed_from_previous` checked by default) that calls `createInvestmentRound`, then selects the new round.
- The selected round's `as_of_date` displayed beside it.
- A running **Total** computed from current input state, so it moves as values are typed.

**Rows** — one per holding from `getInvestmentHoldings()` filtered to `closed === 0`, grouped by `owner`, each showing:
- previous round's value (read-only, from `getInvestmentSnapshot()` — the second-to-last cell of that holding's `values`),
- a controlled numeric input for the new value,
- delta in dollars and percent vs. the previous value,
- a date input defaulting to the round's `as_of_date`.

Render by `kind`:
- `property` — market value and debt inputs; equity shown as the computed difference and submitted as `value_cents`.
- `crypto` — units and unit price inputs; value shown as the product and submitted as `value_cents`.
- everything else — one amount input.
- A holding whose previous value included `vested_cents` also gets a vested input.

Seeded rows (previous-round values carried forward) render muted with a "carried forward" chip until edited. This is the state that produced the stale Marcus number — visible now instead of silent.

**Footer** — **Save round** calls `saveInvestmentValues` once with every row, then:

```ts
qc.invalidateQueries({ queryKey: ["investments_snapshot"] });
qc.invalidateQueries({ queryKey: ["investments_rounds"] });
qc.invalidateQueries({ queryKey: ["investments_holdings"] });
```

**Manage holdings modal** — add / rename / close, calling `saveHolding`. Closing sets `closed: true`; there is no delete.

Follow the house rule that the UI shows outcomes, not internals: no source flags, no import mechanics, no algorithm detail on screen.

- [ ] **Step 4: Register the route**

In `src/App.tsx`:
- import `InvestmentsUpdate` alongside the other Investments imports (~line 40),
- add `{ to: "/investments/update", icon: PencilLine, label: "Update Values" }` to the Investments sidebar group (~line 90),
- add `<Route path="/investments/update" element={<InvestmentsUpdate />} />` (~line 189).

- [ ] **Step 5: Typecheck and build**

Run from `C:\Users\Steven\ynabhelper-ui`:
`npx tsc --noEmit && npm run build`
Expected: both clean.

- [ ] **Step 6: Manual verification against the live bot**

With the bot running, open the app, go to Investments → Update Values. Confirm: the Jul 2026 round loads with 23 active holdings pre-filled at their stored values; changing one updates the running total; **Save round** succeeds and Overview reflects the change.

Then the regression that matters: **open the screen and save without editing anything.** Every value must come back unchanged. If any row saves as $0.00, the draft-seeding effect isn't running.

- [ ] **Step 7: Commit**

```bash
git add src/pages/InvestmentsUpdate.tsx src/App.tsx
git commit -m "feat(investments): Update Values round editor"
```

---

## Task 10: Insurance editor screen

**Files:**
- Create: `ynabhelper-ui/src/pages/InvestmentsInsuranceEdit.tsx`
- Modify: `ynabhelper-ui/src/App.tsx`

**Interfaces:**
- Consumes: `getInsurancePolicies`, `savePolicy`, `recordPremium`
- Produces: route `/investments/insurance/edit`

- [ ] **Step 1: Build the screen**

Create `src/pages/InvestmentsInsuranceEdit.tsx`. One table over `getInsurancePolicies()`, columns:

`Type · Provider · Coverage · Deductible · Expected (annualized) · Observed · Drift · Last verified · Paid via`

- **Drift** shows the signed difference, red when positive (premium rose), with the percent beside it. A +42% jump is the case this column exists for.
- **`unverified`** rows show an amber "unverified" chip in the Last-verified column rather than a zero drift — no observation and no drift are different facts.
- **`paid_via === "escrow"`** rows show an "escrow" chip, since those premiums never appear in the ledger and can only arrive by hand.
- **Record premium** button per row opens a small form (`amount`, `as_of_date`, `source` defaulting to `escrow` for escrow-paid policies, `note`) posting via `recordPremium`.
- **Edit** expands an inline form over the policy fields, posting via `savePolicy`. Deactivating sets `active: false`; there is no delete.

Both mutations invalidate `["insurance_policies"]` and `["investments_snapshot"]`.

- [ ] **Step 2: Register the route**

In `src/App.tsx`: import the page, add `{ to: "/investments/insurance/edit", icon: ShieldCheck, label: "Insurance Editor" }` to the Investments group, and add the `<Route>`.

- [ ] **Step 3: Typecheck and build**

Run: `npx tsc --noEmit && npm run build`
Expected: clean.

- [ ] **Step 4: Manual verification**

Confirm the Amica homeowners row shows expected $2,105.00, observed $2,798.00, drift +$693.00 / +32.9% once the current premium is recorded.

- [ ] **Step 5: Commit**

```bash
git add src/pages/InvestmentsInsuranceEdit.tsx src/App.tsx
git commit -m "feat(investments): insurance editor with premium drift"
```

---

## Task 11: Snapshot date on the existing pages

**Files:**
- Modify: `ynabhelper-ui/src/pages/InvestmentsOverview.tsx`, `InvestmentsHoldings.tsx`, `InvestmentsAllocation.tsx`, `InvestmentsInsurance.tsx`

**Interfaces:**
- Consumes: `getInvestmentSnapshot` (now returns a real `as_of`), `getInvestmentRounds`
- Produces: no new exports

- [ ] **Step 1: Add the as-of chip to all four pages**

Each page already queries `["investments_snapshot"]`. Add to the `PageShell` subtitle: `as of {formatDate(snap.as_of)}`, and drop the "Snapshot data from your spreadsheet" copy — it is no longer true. Handle `as_of === null` (empty DB) with the existing empty state.

- [ ] **Step 2: Add the round selector to Overview**

In `InvestmentsOverview.tsx`, add a `<select>` over `getInvestmentRounds()` that sets a `roundId` state, passed through to `getInvestmentSnapshot`. Extend the client function to take it:

```ts
export function getInvestmentSnapshot(roundId?: string): Promise<InvestmentSnapshot> {
  const qs = roundId ? `?round_id=${encodeURIComponent(roundId)}` : "";
  return apiGet<InvestmentSnapshot>(`/investments/snapshot${qs}`);
}
```

Include `roundId` in the query key: `["investments_snapshot", roundId ?? "latest"]`. Update the other three pages' query keys to match, passing `undefined`.

- [ ] **Step 3: Simplify `buildSeries()`**

In `InvestmentsOverview.tsx`, this line exists only because the xlsx parser compressed value arrays:

```js
const seed = snap.holdings.find((h) => h.values.length === totalRow.cells.length);
```

The DB emits round-aligned arrays for every holding, so replace the seed hunt with `snap.holdings[0]` (guarding the empty case). Verify the chart still renders across all 8 rounds.

- [ ] **Step 4: Typecheck and build**

Run: `npx tsc --noEmit && npm run build`
Expected: clean.

- [ ] **Step 5: Manual verification**

Open each of the four pages. Each shows "as of 2026-07-25". On Overview, selecting Feb 2026 truncates the chart and totals to that round.

- [ ] **Step 6: Commit**

```bash
git add src/pages/InvestmentsOverview.tsx src/pages/InvestmentsHoldings.tsx \
        src/pages/InvestmentsAllocation.tsx src/pages/InvestmentsInsurance.tsx src/lib/api.ts
git commit -m "feat(investments): snapshot date chip and round selector"
```

---

## Task 12: Deploy

**Files:** none — deployment only.

- [ ] **Step 1: Full test suite**

Run from `C:\Users\Steven\ynabhelper`: `python -m pytest tests/ -q`
Expected: all pass.

- [ ] **Step 2: Restart the bot**

Restart the `YNAB-Helper-Bot` scheduled task — via the app's Core → Bot Control panel, or `schtasks /End` + `/Run`. Never PID-kill it, and never start a bot inside this session: the scheduled task owns the lifecycle and a second process fights for port 8765.

- [ ] **Step 3: Confirm the new routes are live**

```bash
curl -s -H "X-API-Token: $(cat C:/Users/Steven/ynabhelper/ui_api_token.txt)" \
     http://127.0.0.1:8765/investments/rounds
```

Expected: 8 rounds, newest `2026-07-25`.

- [ ] **Step 4: Hot-swap the desktop app**

From `C:\Users\Steven\ynabhelper-ui`: `npm run tauri build`, then copy the built exe over the installed one in `AppData\Local` ("Harris Budget"). No reinstall needed.

- [ ] **Step 5: Enter the known gaps**

Through the new editor, so the first real use exercises the write path:
- Optum HSA balance (row exists, value blank).
- Bitcoin exact unit count (currently 0.947 implied).
- Escrow: either add 117 Mayfield's $3,971.14 as a `cash` holding, or leave it out and note the decision.
- Current Amica / Fortegra / Neptune premiums as `escrow` observations.

- [ ] **Step 6: Final verification**

Run: `python scripts/verify_investments_import.py`
Expected: still OK for the imported rounds. New hand-entered values legitimately diverge from the xlsx — that divergence is the migration succeeding, and from here the xlsx is a frozen historical artifact, not a source.

- [ ] **Step 7: Commit any deployment notes**

```bash
git add -A && git commit -m "chore(investments): cutover to DB-backed system of record"
```

---

## Rollback

The xlsx files stay untouched in `G:\My Drive\ynabclone\investments\` and `bot/investments.py` stays importable, so rollback is `git revert` of the Task 6 route change plus a bot restart — the data is still where it was. No feature flag; the revert is simpler than the flag would be. The pre-import DB backup from Task 7 Step 2 covers the schema side.
