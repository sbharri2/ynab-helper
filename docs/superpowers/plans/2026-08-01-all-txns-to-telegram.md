# All Transactions to Telegram + Auto-Sync Panel — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every transaction reaches Telegram — charges matching a user-authored rule arrive as an FYI that needs no reply, everything else asks — with a new Auto-Sync panel for creating those rules from discovered spending patterns.

**Architecture:** A new `bot/dispatch.py` becomes the single owner of "what reaches Telegram." Ingest and YNAB sync stop making category decisions and only enqueue. Dispatch classifies each `pending_txn` against a DB-backed `auto_rule` table: a match files it and sends an FYI; a miss leaves it pending for the existing group-ping question flow. The learned-prior logic stops auto-filing and instead powers a suggestion list in the panel.

**Tech Stack:** Python 3.11, SQLite (`bot/storage.py` DDL block), python-telegram-bot, FastAPI (`bot/http_api.py`, loopback only), pytest, React + TypeScript + Tauri (`../ynabhelper-ui`).

## Global Constraints

- **Amazon is out of scope.** Do not modify `_amazon_bucket_category`, the `Amazon - *` bucket categories, the large-Amazon HOLD lane, or `queue_lane.LARGE_AMAZON_*`. Another effort owns that revamp; colliding edits will conflict.
- **The bot is the single writer.** All UI mutations go through `bot/http_api.py` endpoints, never direct DB writes from the Tauri app.
- **FastAPI Pydantic body models MUST be declared at module scope**, never inside the `build_app()` closure. A model defined in the closure makes every POST to that route return 422.
- **Never PID-kill or start the bot.** It runs as the `YNAB-Helper-Bot` scheduled task. Code goes live only when that task restarts.
- **Money sums filter `is_split = 0`; registers filter `parent_txn_id IS NULL`.** Splits are stored as parent + child rows.
- **Never bulk-recompute `month_category` for historical months.** It produces phantom Ready-to-Assign movement (measured at +$10.5k).
- **`payee NOT LIKE 'Transfer :%'`** — credit cards are paid in full monthly; transfer rows are movement, not spend.
- **Off-budget accounts are excluded** via `account_id IN (SELECT id FROM account WHERE on_budget = 1)`.
- Run tests with `./.venv/Scripts/python.exe -m pytest`. PowerShell blocks `.ps1`; pair any script with `Set-ExecutionPolicy -Scope Process Bypass -Force`.
- The live DB is `C:/Users/Steven/ynabhelper/ynab_helper.db`. Never query the stale copy on `G:`.

---

### Task 1: `auto_rule` table and storage helpers

**Files:**
- Modify: `bot/storage.py` (SCHEMA block near line 99; helpers after `insert_pending_txn` at line 759)
- Test: `tests/test_auto_rule.py`

**Interfaces:**
- Consumes: `storage.connect`, `storage.init_db`, `storage._utcnow`
- Produces:
  - `storage.list_auto_rules(db_path, *, enabled_only: bool = False) -> list[dict]`
  - `storage.create_auto_rule(db_path, *, pattern: str, category_id: str, created_by: str, note: str | None = None) -> int`
  - `storage.update_auto_rule(db_path, rule_id: int, *, pattern: str | None = None, category_id: str | None = None, enabled: bool | None = None, note: str | None = None) -> bool`
  - `storage.delete_auto_rule(db_path, rule_id: int) -> bool`
  - `storage.bump_auto_rule_fire(db_path, rule_id: int) -> None`

- [ ] **Step 1: Write the failing test**

Create `tests/test_auto_rule.py`:

```python
import sqlite3

import pytest

from bot import storage
from bot.storage import init_db


def _seed_category(db, cat_id="cat-groceries", name="Groceries"):
    with storage.connect(db) as con:
        con.execute(
            "INSERT OR IGNORE INTO category_group (id, name) VALUES (?, ?)",
            ("grp-1", "Day to Day Expenses"),
        )
        con.execute(
            "INSERT OR IGNORE INTO category (id, group_id, name) VALUES (?, ?, ?)",
            (cat_id, "grp-1", name),
        )
    return cat_id


def test_auto_rule_table_exists(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    con = sqlite3.connect(db)
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "auto_rule" in tables


def test_create_and_list(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    cat = _seed_category(db)
    rid = storage.create_auto_rule(
        db, pattern=r"\bHARRIS\s*TEETER\b", category_id=cat,
        created_by="steven", note="weekly groceries",
    )
    assert rid > 0
    rules = storage.list_auto_rules(db)
    assert len(rules) == 1
    assert rules[0]["pattern"] == r"\bHARRIS\s*TEETER\b"
    assert rules[0]["category_id"] == cat
    assert rules[0]["enabled"] == 1
    assert rules[0]["fire_count"] == 0
    assert rules[0]["created_by"] == "steven"


def test_duplicate_pattern_rejected(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    cat = _seed_category(db)
    storage.create_auto_rule(db, pattern=r"\bAPPLE\b", category_id=cat,
                             created_by="steven")
    with pytest.raises(sqlite3.IntegrityError):
        storage.create_auto_rule(db, pattern=r"\bAPPLE\b", category_id=cat,
                                 created_by="steven")


def test_enabled_only_filter(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    cat = _seed_category(db)
    a = storage.create_auto_rule(db, pattern=r"\bA\b", category_id=cat,
                                 created_by="seed")
    storage.create_auto_rule(db, pattern=r"\bB\b", category_id=cat,
                             created_by="seed")
    storage.update_auto_rule(db, a, enabled=False)
    assert len(storage.list_auto_rules(db)) == 2
    assert len(storage.list_auto_rules(db, enabled_only=True)) == 1


def test_update_and_delete(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    cat = _seed_category(db)
    other = _seed_category(db, "cat-dining", "Dining Out/Entertainment")
    rid = storage.create_auto_rule(db, pattern=r"\bX\b", category_id=cat,
                                   created_by="steven")
    assert storage.update_auto_rule(db, rid, category_id=other) is True
    assert storage.list_auto_rules(db)[0]["category_id"] == other
    assert storage.delete_auto_rule(db, rid) is True
    assert storage.list_auto_rules(db) == []
    assert storage.delete_auto_rule(db, rid) is False


def test_bump_fire_count(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    cat = _seed_category(db)
    rid = storage.create_auto_rule(db, pattern=r"\bY\b", category_id=cat,
                                   created_by="steven")
    storage.bump_auto_rule_fire(db, rid)
    storage.bump_auto_rule_fire(db, rid)
    row = storage.list_auto_rules(db)[0]
    assert row["fire_count"] == 2
    assert row["last_fired_at"] is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_auto_rule.py -v`
Expected: FAIL — `assert "auto_rule" in tables` fails, and `AttributeError: module 'bot.storage' has no attribute 'create_auto_rule'`.

- [ ] **Step 3: Add the DDL**

In `bot/storage.py`, inside the `SCHEMA` string, immediately after the
`income_source_override` table (ends line 109) and before the
`CREATE INDEX IF NOT EXISTS idx_pending_order_status` line:

```sql
-- Auto-Sync rules (2026-08-01). Payee pattern -> category, authored by the
-- user in the Auto-Sync panel. Replaces the hard-coded OVERRIDES list in
-- bot/payee_overrides.py, which needed a code edit plus a bot restart to
-- change one bill's category. A matching rule files the charge and sends an
-- FYI; no rule means the charge asks. First enabled rule by ascending id
-- wins -- deliberately no priority scoring, so a wrong filing is always
-- explainable by pointing at exactly one rule.
CREATE TABLE IF NOT EXISTS auto_rule (
  id            INTEGER PRIMARY KEY,
  pattern       TEXT NOT NULL UNIQUE,
  category_id   TEXT NOT NULL REFERENCES category(id),
  enabled       INTEGER NOT NULL DEFAULT 1,
  note          TEXT,
  created_by    TEXT,
  created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  last_fired_at TIMESTAMP,
  fire_count    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_auto_rule_enabled ON auto_rule(enabled, id);
```

- [ ] **Step 4: Add the helpers**

In `bot/storage.py`, after `insert_pending_txn` (which ends at line 759) and
before `list_unmatched_pending_orders`:

```python
def list_auto_rules(db_path: Path | str, *,
                    enabled_only: bool = False) -> list[dict]:
    """All Auto-Sync rules in match order (ascending id).

    Match order is id order, not fire_count order — the panel sorts by
    fire_count for display, but matching must stay deterministic and
    independent of how often a rule has fired.
    """
    sql = "SELECT * FROM auto_rule"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY id ASC"
    with connect(db_path) as con:
        return [dict(r) for r in con.execute(sql)]


def create_auto_rule(db_path: Path | str, *, pattern: str, category_id: str,
                     created_by: str, note: str | None = None) -> int:
    """Insert a rule. Raises sqlite3.IntegrityError on duplicate pattern —
    callers surface that as a user-facing 'this rule already exists'."""
    with connect(db_path) as con:
        cur = con.execute(
            "INSERT INTO auto_rule (pattern, category_id, created_by, note) "
            "VALUES (?, ?, ?, ?)",
            (pattern, category_id, created_by, note),
        )
        return cur.lastrowid


def update_auto_rule(db_path: Path | str, rule_id: int, *,
                     pattern: str | None = None,
                     category_id: str | None = None,
                     enabled: bool | None = None,
                     note: str | None = None) -> bool:
    """Patch the supplied fields. Returns False when no such rule."""
    sets, args = [], []
    if pattern is not None:
        sets.append("pattern = ?"); args.append(pattern)
    if category_id is not None:
        sets.append("category_id = ?"); args.append(category_id)
    if enabled is not None:
        sets.append("enabled = ?"); args.append(1 if enabled else 0)
    if note is not None:
        sets.append("note = ?"); args.append(note)
    if not sets:
        return False
    args.append(rule_id)
    with connect(db_path) as con:
        cur = con.execute(
            f"UPDATE auto_rule SET {', '.join(sets)} WHERE id = ?", args,
        )
        return cur.rowcount > 0


def delete_auto_rule(db_path: Path | str, rule_id: int) -> bool:
    with connect(db_path) as con:
        cur = con.execute("DELETE FROM auto_rule WHERE id = ?", (rule_id,))
        return cur.rowcount > 0


def bump_auto_rule_fire(db_path: Path | str, rule_id: int) -> None:
    """Record that a rule filed a transaction. Drives the panel's
    'fired N times / last fired' columns so dead rules are visible."""
    with connect(db_path) as con:
        con.execute(
            "UPDATE auto_rule SET fire_count = fire_count + 1, "
            "last_fired_at = ? WHERE id = ?",
            (_utcnow(), rule_id),
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_auto_rule.py -v`
Expected: 6 passed.

- [ ] **Step 6: Verify existing suite still green**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_storage.py -q`
Expected: all pass (the DDL addition is additive and idempotent).

- [ ] **Step 7: Commit**

```bash
git add bot/storage.py tests/test_auto_rule.py
git commit -m "feat(auto-sync): auto_rule table and storage helpers"
```

---

### Task 2: Rule matching reads the DB; seed the 40 hard-coded overrides

**Files:**
- Modify: `bot/payee_overrides.py` (replace `resolve_payee_override`; keep `OVERRIDES` as seed data only)
- Create: `scripts/seed_auto_rules.py`
- Test: `tests/test_auto_rule_match.py`

**Interfaces:**
- Consumes: `storage.list_auto_rules`, `storage.create_auto_rule`
- Produces:
  - `payee_overrides.match_rule(db_path, payee: str) -> dict | None` returning `{"rule_id", "category_id", "pattern"}`
  - `payee_overrides.resolve_payee_override(db_path, payee)` — unchanged public shape, returns `{"category_id", "category_name", "source", "rule"}` or `None`
  - `scripts/seed_auto_rules.py` CLI with `--db` and `--dry-run`

- [ ] **Step 1: Write the failing test**

Create `tests/test_auto_rule_match.py`:

```python
from bot import payee_overrides, storage
from bot.storage import init_db


def _cat(db, cat_id, name):
    with storage.connect(db) as con:
        con.execute("INSERT OR IGNORE INTO category_group (id, name) "
                    "VALUES ('g', 'G')")
        con.execute("INSERT OR IGNORE INTO category (id, group_id, name) "
                    "VALUES (?, 'g', ?)", (cat_id, name))
    return cat_id


def test_match_returns_none_with_no_rules(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    assert payee_overrides.match_rule(db, "HARRIS TEETER 0123") is None


def test_match_is_case_insensitive(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    cat = _cat(db, "c-groc", "Groceries")
    rid = storage.create_auto_rule(db, pattern=r"\bHARRIS\s*TEETER\b",
                                   category_id=cat, created_by="steven")
    m = payee_overrides.match_rule(db, "harris teeter #0123 APEX USA")
    assert m is not None
    assert m["rule_id"] == rid
    assert m["category_id"] == cat


def test_lowest_id_wins(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    a = _cat(db, "c-a", "Groceries")
    b = _cat(db, "c-b", "Dining Out/Entertainment")
    first = storage.create_auto_rule(db, pattern=r"HARRIS", category_id=a,
                                     created_by="seed")
    storage.create_auto_rule(db, pattern=r"HARRIS\s*TEETER", category_id=b,
                             created_by="steven")
    assert payee_overrides.match_rule(db, "HARRIS TEETER")["rule_id"] == first


def test_disabled_rule_never_fires(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    cat = _cat(db, "c-groc", "Groceries")
    rid = storage.create_auto_rule(db, pattern=r"HARRIS", category_id=cat,
                                   created_by="steven")
    storage.update_auto_rule(db, rid, enabled=False)
    assert payee_overrides.match_rule(db, "HARRIS TEETER") is None


def test_invalid_regex_is_skipped_not_fatal(tmp_path):
    """A rule saved with a bad pattern must not break matching for the
    rules after it — one malformed row cannot take down categorization."""
    db = tmp_path / "t.db"
    init_db(db)
    cat = _cat(db, "c-groc", "Groceries")
    with storage.connect(db) as con:
        con.execute("INSERT INTO auto_rule (pattern, category_id, created_by) "
                    "VALUES ('[unclosed', ?, 'steven')", (cat,))
    storage.create_auto_rule(db, pattern=r"HARRIS", category_id=cat,
                             created_by="steven")
    m = payee_overrides.match_rule(db, "HARRIS TEETER")
    assert m is not None and m["category_id"] == cat


def test_resolve_payee_override_shape_preserved(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    cat = _cat(db, "c-elec", "Electric (24th)")
    storage.create_auto_rule(db, pattern=r"\bDUKE\s*ENERGY\b",
                             category_id=cat, created_by="seed")
    got = payee_overrides.resolve_payee_override(db, "DUKE ENERGY 800-777")
    assert got["category_id"] == cat
    assert got["category_name"] == "Electric (24th)"
    assert got["source"] == "payee_override"


def test_empty_payee(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    assert payee_overrides.match_rule(db, "") is None
    assert payee_overrides.match_rule(db, None) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_auto_rule_match.py -v`
Expected: FAIL — `AttributeError: module 'bot.payee_overrides' has no attribute 'match_rule'`.

- [ ] **Step 3: Rewrite the matching functions**

In `bot/payee_overrides.py`, keep the `OVERRIDES` list exactly as-is (Task 2
Step 5 uses it as seed data) but replace `resolve_payee_override` (lines
101-134) with:

```python
def match_rule(db_path: Path | str, payee: str | None) -> dict | None:
    """First enabled auto_rule whose pattern matches ``payee``.

    Match order is ascending id — the same order list_auto_rules returns.
    A rule with an uncompilable pattern is logged and skipped rather than
    raised: one malformed row must not take down categorization for every
    transaction behind it.
    """
    if not payee:
        return None
    for rule in storage.list_auto_rules(db_path, enabled_only=True):
        try:
            pat = re.compile(rule["pattern"], re.I)
        except re.error as e:
            log.warning("auto_rule %s has an invalid pattern %r: %s",
                        rule["id"], rule["pattern"], e)
            continue
        if pat.search(payee):
            return {
                "rule_id": rule["id"],
                "category_id": rule["category_id"],
                "pattern": rule["pattern"],
            }
    return None


def resolve_payee_override(db_path: Path | str, payee: str) -> dict | None:
    """Back-compatible wrapper: returns the matched rule's category with
    its display name, or None. Existing callers in bot/ingest.py and
    bot/suggest.py use this shape.
    """
    m = match_rule(db_path, payee)
    if m is None:
        return None
    with storage.connect(db_path) as con:
        row = con.execute(
            "SELECT name FROM category WHERE id = ? AND hidden = 0",
            (m["category_id"],),
        ).fetchone()
    if row is None:
        log.warning("auto_rule %s points at missing/hidden category %s",
                    m["rule_id"], m["category_id"])
        return None
    return {
        "category_id": m["category_id"],
        "category_name": row["name"],
        "source": "payee_override",
        "rule": m["pattern"],
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_auto_rule_match.py -v`
Expected: 7 passed.

- [ ] **Step 5: Write the seed script**

Create `scripts/seed_auto_rules.py`:

```python
"""One-shot: copy bot/payee_overrides.OVERRIDES into the auto_rule table.

After this runs, the Auto-Sync panel owns these rules and the OVERRIDES
literal can be deleted. A rule whose category no longer exists in the DB is
REPORTED, not silently dropped -- a missing envelope is a data problem worth
seeing, and swallowing it would quietly stop a bill from auto-filing.

Usage:
    ./.venv/Scripts/python.exe scripts/seed_auto_rules.py --dry-run
    ./.venv/Scripts/python.exe scripts/seed_auto_rules.py
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import storage                      # noqa: E402
from bot.payee_overrides import OVERRIDES    # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="ynab_helper.db")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    db = Path(args.db)

    existing = {r["pattern"] for r in storage.list_auto_rules(db)}
    created = skipped_dupe = 0
    missing: list[tuple[str, str]] = []

    for rule in OVERRIDES:
        pattern = rule.pattern.pattern
        if pattern in existing:
            skipped_dupe += 1
            continue
        with storage.connect(db) as con:
            row = con.execute(
                "SELECT id FROM category WHERE LOWER(name) = LOWER(?) "
                "AND hidden = 0", (rule.category_name,),
            ).fetchone()
        if row is None:
            missing.append((pattern, rule.category_name))
            continue
        print(f"  {pattern[:46]:46} -> {rule.category_name}")
        if not args.dry_run:
            try:
                storage.create_auto_rule(
                    db, pattern=pattern, category_id=row["id"],
                    created_by="seed",
                    note="migrated from payee_overrides.OVERRIDES",
                )
            except sqlite3.IntegrityError:
                skipped_dupe += 1
                continue
        created += 1

    print(f"\ncreated={created} already_present={skipped_dupe} "
          f"missing_category={len(missing)}")
    for pattern, name in missing:
        print(f"  MISSING CATEGORY {name!r} for pattern {pattern!r}")
    if args.dry_run:
        print("(dry-run: nothing written)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 6: Dry-run the seed against the live DB**

Run: `./.venv/Scripts/python.exe scripts/seed_auto_rules.py --dry-run`
Expected: ~40 lines of `pattern -> Category`, then a summary. Investigate any
`MISSING CATEGORY` line before proceeding — it means a category was renamed.

- [ ] **Step 7: Run the seed for real**

Run: `./.venv/Scripts/python.exe scripts/seed_auto_rules.py`
Expected: `created=40 already_present=0 missing_category=0` (adjust for any
genuine misses found in Step 6).

Verify: `./.venv/Scripts/python.exe -c "from bot import storage; print(len(storage.list_auto_rules('ynab_helper.db')))"`
Expected: `40`

- [ ] **Step 8: Delete the OVERRIDES literal**

In `bot/payee_overrides.py`, delete the `_Rule` NamedTuple and the entire
`OVERRIDES` list (lines 33-98), and update the module docstring to describe
the table-backed design. Keep the `re` import — `match_rule` uses it.

Verify nothing else imports it:

Run: `grep -rn "OVERRIDES\|_Rule" --include=*.py bot/ tests/`
Expected: only `scripts/seed_auto_rules.py` (which has now served its purpose
and may keep the stale import — move it to `scripts/archive/` instead).

```bash
git mv scripts/seed_auto_rules.py scripts/archive/seed_auto_rules.py
```

- [ ] **Step 9: Run the full suite**

Run: `./.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: all pass.

- [ ] **Step 10: Commit**

```bash
git add bot/payee_overrides.py scripts/archive/seed_auto_rules.py tests/test_auto_rule_match.py
git commit -m "feat(auto-sync): rule matching reads auto_rule table; seed the 40 overrides"
```

---

### Task 3: `bot/dispatch.py` — classification and rule filing

**Files:**
- Create: `bot/dispatch.py`
- Test: `tests/test_dispatch.py`

**Interfaces:**
- Consumes: `payee_overrides.match_rule`, `storage.bump_auto_rule_fire`, `storage.connect`, `storage.audit`
- Produces:
  - `dispatch.classify(db_path, pt_id: int) -> dict` returning `{"action": "fyi" | "ask", "pt_id": int, "rule_id": int | None, "category_id": str | None}`
  - `dispatch.file_by_rule(db_path, pt_id: int, rule_id: int, category_id: str) -> bool`
  - `dispatch.dispatch_pending(db_path) -> dict` — classify every `status='pending'` row with no decision yet, file the rule hits, return `{"filed": int, "asked": int}`

- [ ] **Step 1: Write the failing test**

Create `tests/test_dispatch.py`:

```python
from datetime import date

from bot import dispatch, storage
from bot.storage import init_db, insert_pending_txn


def _setup(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    with storage.connect(db) as con:
        con.execute("INSERT INTO category_group (id, name) VALUES ('g','G')")
        con.execute("INSERT INTO category (id, group_id, name) "
                    "VALUES ('c-elec','g','Electric (24th)')")
        con.execute("INSERT INTO account (id, name, type, on_budget) "
                    "VALUES ('a1','Joint Checking','checking',1)")
    return db


def _ledger(db, payee, cents=-18400):
    with storage.connect(db) as con:
        cur = con.execute(
            "INSERT INTO ledger_txn (account_id, posted_date, amount_cents, "
            "payee, is_split) VALUES ('a1', '2026-08-01', ?, ?, 0)",
            (cents, payee),
        )
        return cur.lastrowid


def _pending(db, payee, lid, cents=-18400):
    return insert_pending_txn(
        db, user_id="steven", ynab_txn_id=f"ledger:{lid}",
        ynab_account_id="a1", payee=payee, amount_cents=cents,
        txn_date=date(2026, 8, 1), memo="",
    )


def test_no_rule_means_ask(tmp_path):
    db = _setup(tmp_path)
    lid = _ledger(db, "SOME NEW MERCHANT")
    pt = _pending(db, "SOME NEW MERCHANT", lid)
    got = dispatch.classify(db, pt)
    assert got["action"] == "ask"
    assert got["rule_id"] is None


def test_rule_hit_means_fyi(tmp_path):
    db = _setup(tmp_path)
    rid = storage.create_auto_rule(db, pattern=r"\bDUKE\s*ENERGY\b",
                                   category_id="c-elec", created_by="steven")
    lid = _ledger(db, "DUKE ENERGY 800-777-9898")
    pt = _pending(db, "DUKE ENERGY 800-777-9898", lid)
    got = dispatch.classify(db, pt)
    assert got["action"] == "fyi"
    assert got["rule_id"] == rid
    assert got["category_id"] == "c-elec"


def test_file_by_rule_writes_pending_and_ledger(tmp_path):
    db = _setup(tmp_path)
    rid = storage.create_auto_rule(db, pattern=r"\bDUKE\b",
                                   category_id="c-elec", created_by="steven")
    lid = _ledger(db, "DUKE ENERGY")
    pt = _pending(db, "DUKE ENERGY", lid)
    assert dispatch.file_by_rule(db, pt, rid, "c-elec") is True
    with storage.connect(db) as con:
        row = con.execute("SELECT status, chosen_category, filed_by "
                          "FROM pending_txn WHERE id = ?", (pt,)).fetchone()
        assert row["status"] == "categorized"
        assert row["chosen_category"] == "c-elec"
        assert row["filed_by"] == f"rule:{rid}"
        lrow = con.execute("SELECT category_id FROM ledger_txn WHERE id = ?",
                           (lid,)).fetchone()
        assert lrow["category_id"] == "c-elec"
        rule = con.execute("SELECT fire_count, last_fired_at FROM auto_rule "
                           "WHERE id = ?", (rid,)).fetchone()
        assert rule["fire_count"] == 1
        assert rule["last_fired_at"] is not None


def test_dispatch_pending_counts(tmp_path):
    db = _setup(tmp_path)
    storage.create_auto_rule(db, pattern=r"\bDUKE\b", category_id="c-elec",
                             created_by="steven")
    for payee in ("DUKE ENERGY", "DUKE ENERGY", "MYSTERY SHOP"):
        lid = _ledger(db, payee)
        _pending(db, payee, lid)
    got = dispatch.dispatch_pending(db)
    assert got == {"filed": 2, "asked": 1}


def test_dispatch_is_idempotent(tmp_path):
    """Running twice must not double-file or double-bump fire_count."""
    db = _setup(tmp_path)
    rid = storage.create_auto_rule(db, pattern=r"\bDUKE\b",
                                   category_id="c-elec", created_by="steven")
    lid = _ledger(db, "DUKE ENERGY")
    _pending(db, "DUKE ENERGY", lid)
    dispatch.dispatch_pending(db)
    second = dispatch.dispatch_pending(db)
    assert second["filed"] == 0
    with storage.connect(db) as con:
        n = con.execute("SELECT fire_count FROM auto_rule WHERE id = ?",
                        (rid,)).fetchone()["fire_count"]
    assert n == 1


def test_ynab_uuid_pending_updates_ledger_by_uuid(tmp_path):
    """Rows whose ynab_txn_id is a real UUID (not 'ledger:N') must still
    promote the category onto the ledger row."""
    db = _setup(tmp_path)
    rid = storage.create_auto_rule(db, pattern=r"\bDUKE\b",
                                   category_id="c-elec", created_by="steven")
    with storage.connect(db) as con:
        con.execute(
            "INSERT INTO ledger_txn (account_id, posted_date, amount_cents, "
            "payee, is_split, ynab_txn_id) "
            "VALUES ('a1','2026-08-01',-18400,'DUKE ENERGY',0,'uuid-1')")
    pt = insert_pending_txn(
        db, user_id="steven", ynab_txn_id="uuid-1", ynab_account_id="a1",
        payee="DUKE ENERGY", amount_cents=-18400,
        txn_date=date(2026, 8, 1), memo="")
    assert dispatch.file_by_rule(db, pt, rid, "c-elec") is True
    with storage.connect(db) as con:
        got = con.execute("SELECT category_id FROM ledger_txn "
                          "WHERE ynab_txn_id = 'uuid-1'").fetchone()
    assert got["category_id"] == "c-elec"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_dispatch.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bot.dispatch'`.

- [ ] **Step 3: Write `bot/dispatch.py`**

```python
"""Single owner of "what reaches Telegram".

Before this module, four independent code paths could each decide a
transaction never reached a phone: ynab_sync rows were never queued, ingest
auto-committed on learned priors, small Amazon charges bucketed silently, and
close_stale_pending adopted whatever category happened to be on the ledger
row. 213 July 2026 outflows produced 41 human decisions.

Now ingest and sync only ENQUEUE. This module decides:

    rule matches payee  -> file it, send an FYI, no reply expected
    no rule             -> leave pending; the group-ping sweep asks

Amazon is deliberately untouched here -- a separate effort owns that revamp.
"""
from __future__ import annotations

import logging
from pathlib import Path

from bot import payee_overrides, storage

log = logging.getLogger(__name__)


def classify(db_path: Path | str, pt_id: int) -> dict:
    """Decide what happens to one pending_txn. Pure: writes nothing."""
    with storage.connect(db_path) as con:
        pt = con.execute(
            "SELECT id, payee FROM pending_txn WHERE id = ?", (pt_id,),
        ).fetchone()
    if pt is None:
        return {"action": "ask", "pt_id": pt_id, "rule_id": None,
                "category_id": None}
    m = payee_overrides.match_rule(db_path, pt["payee"])
    if m is None:
        return {"action": "ask", "pt_id": pt_id, "rule_id": None,
                "category_id": None}
    return {"action": "fyi", "pt_id": pt_id, "rule_id": m["rule_id"],
            "category_id": m["category_id"]}


def file_by_rule(db_path: Path | str, pt_id: int, rule_id: int,
                 category_id: str) -> bool:
    """Commit a rule's category onto the pending row and its ledger row.

    Mirrors group_chat._file_item so a rule filing and a human filing leave
    the row in exactly the same shape -- the only difference is filed_by.
    synced_to_ynab_at is left NULL so ynab_writer picks the row up.
    """
    with storage.connect(db_path) as con:
        pt = con.execute(
            "SELECT id, ynab_txn_id, status FROM pending_txn WHERE id = ?",
            (pt_id,),
        ).fetchone()
        if pt is None or pt["status"] != "pending":
            return False
        con.execute(
            "UPDATE pending_txn SET chosen_category = ?, chosen_at = ?, "
            "status = 'categorized', filed_by = ?, "
            "synced_to_ynab_at = NULL WHERE id = ?",
            (category_id, storage._utcnow(), f"rule:{rule_id}", pt_id),
        )
        yid = pt["ynab_txn_id"] or ""
        if yid.startswith("ledger:"):
            try:
                lid = int(yid.split(":", 1)[1])
                con.execute(
                    "UPDATE ledger_txn SET category_id = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (category_id, lid),
                )
            except (ValueError, IndexError):
                pass
        elif yid:
            con.execute(
                "UPDATE ledger_txn SET category_id = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE ynab_txn_id = ?",
                (category_id, yid),
            )
    storage.bump_auto_rule_fire(db_path, rule_id)
    storage.audit(db_path, "rule_filed", {
        "pending_txn_id": pt_id, "rule_id": rule_id,
        "category_id": category_id,
    })
    return True


def dispatch_pending(db_path: Path | str) -> dict:
    """Classify every undecided pending row; file the rule hits.

    Idempotent: file_by_rule's status guard means a second pass over an
    already-filed row is a no-op, so fire_count cannot double-count.
    """
    with storage.connect(db_path) as con:
        ids = [r["id"] for r in con.execute(
            "SELECT id FROM pending_txn WHERE status = 'pending' "
            "ORDER BY id ASC")]
    filed = asked = 0
    for pt_id in ids:
        got = classify(db_path, pt_id)
        if got["action"] == "fyi" and file_by_rule(
                db_path, pt_id, got["rule_id"], got["category_id"]):
            filed += 1
        else:
            asked += 1
    if filed:
        log.info("dispatch_pending: filed %d by rule, %d left to ask",
                 filed, asked)
    return {"filed": filed, "asked": asked}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_dispatch.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add bot/dispatch.py tests/test_dispatch.py
git commit -m "feat(auto-sync): bot/dispatch.py owns the ask-vs-file decision"
```

---

### Task 4: Ingest stops deciding; `ynab_sync` gets queued

**Files:**
- Modify: `bot/ingest.py:63-71` (`_PROMPT_USER_KINDS`), `bot/ingest.py:386-430` (auto-commit block)
- Test: `tests/test_ingest_no_autofile.py`

**Interfaces:**
- Consumes: nothing new
- Produces: `ingest_signal` return dict unchanged (`action`, `ledger_txn_id`, `ledger_signal_id`, `category_id`, `pending_txn_id`); `pending_txn.status` is now always `'pending'` on creation

- [ ] **Step 1: Write the failing test**

Create `tests/test_ingest_no_autofile.py`:

```python
from bot import storage
from bot.ingest import _PROMPT_USER_KINDS


def test_ynab_sync_is_a_prompt_kind():
    """Regression: bot/ynab_watcher.py was deleted in the Phase 7+ cleanup,
    but ingest.py's comment still claimed it queued ynab_sync rows. Every
    YNAB-sourced transaction since then had no question path at all."""
    assert "ynab_sync" in _PROMPT_USER_KINDS


def test_auto_commit_block_is_gone():
    """Ingest must no longer set status='categorized' at ingest time.
    Dispatch owns that decision now."""
    src = open("bot/ingest.py", encoding="utf-8").read()
    assert "auto_commit" not in src
    assert "auto_prior" not in src
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_ingest_no_autofile.py -v`
Expected: both FAIL.

- [ ] **Step 3: Add `ynab_sync` to the prompt kinds**

In `bot/ingest.py`, replace lines 63-71 with:

```python
# Signal kinds that ALSO write a pending_txn row when ingest creates a new
# ledger_txn. Every kind that can produce a real charge belongs here: as of
# 2026-08-01 nothing files without reaching Telegram first.
#
# ynab_sync was previously excluded, with a comment claiming
# ynab_watcher.poll_once queued those rows instead. bot/ynab_watcher.py was
# deleted in the Phase 7+ writer redesign, so that comment guarded a hole
# rather than documenting a decision -- 40 of 213 July 2026 outflows never
# entered the queue at all.
_PROMPT_USER_KINDS = {
    "chase_alert", "citi_alert", "coastal_transaction_alert",
    "coastal_check_cleared", "paypal_payment", "ynab_sync",
}
```

- [ ] **Step 4: Strip the auto-commit block**

In `bot/ingest.py`, replace the whole `if pending_txn_id and category_id:`
block (lines 386-430) with:

```python
            if pending_txn_id and category_id:
                # Store the categorizer's pick as a SUGGESTION only. Ingest
                # no longer commits: bot/dispatch.py decides whether this
                # row files by rule (FYI) or asks. The suggestion pre-fills
                # the question's buttons.
                with storage.connect(db_path) as con:
                    con.execute(
                        "UPDATE pending_txn SET suggested_category = ?, "
                        "raw_summary = ? WHERE id = ?",
                        (category_id, parsed.get("summary") or "",
                         pending_txn_id),
                    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_ingest_no_autofile.py -v`
Expected: 2 passed.

- [ ] **Step 6: Run the suites that exercise ingest**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_large_amazon.py tests/test_categorizer.py tests/test_matcher.py -q`
Expected: all pass. If a large-Amazon test asserted on `filed_by="auto_override"`,
update the assertion to expect `status='pending'` — but do **not** change any
Amazon bucketing behavior itself.

- [ ] **Step 7: Commit**

```bash
git add bot/ingest.py tests/test_ingest_no_autofile.py
git commit -m "fix(ingest): stop auto-filing; queue ynab_sync rows

Closes the hole left by the deleted ynab_watcher — 40 of 213 July
outflows never entered the queue. Ingest now stores a suggestion and
lets bot/dispatch.py decide file-vs-ask."
```

---

### Task 5: `close_stale_pending` narrows; YNAB adoption becomes visible

**Files:**
- Modify: `bot/ynab_full_sync.py:549-600` (`close_stale_pending`), and the `_upsert_txn` new-row path
- Test: `tests/test_close_stale_pending.py`

**Interfaces:**
- Consumes: `dispatch.dispatch_pending`
- Produces: `close_stale_pending(db_path) -> int` (unchanged signature, narrowed behavior); rows adopted from YNAB are stamped `filed_by='ynab'`

- [ ] **Step 1: Write the failing test**

Create `tests/test_close_stale_pending.py`:

```python
from datetime import date

from bot import storage
from bot.storage import init_db, insert_pending_txn
from bot.ynab_full_sync import close_stale_pending


def _setup(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    with storage.connect(db) as con:
        con.execute("INSERT INTO category_group (id, name) VALUES ('g','G')")
        con.execute("INSERT INTO category (id, group_id, name) "
                    "VALUES ('c1','g','Groceries')")
        con.execute("INSERT INTO account (id, name, type, on_budget) "
                    "VALUES ('a1','Chk','checking',1)")
    return db


def test_categorized_ledger_no_longer_silently_closes(tmp_path):
    """The janitor's category-adoption branch is gone. A pending row whose
    ledger txn has a category must STAY pending so it still reaches Telegram."""
    db = _setup(tmp_path)
    with storage.connect(db) as con:
        cur = con.execute(
            "INSERT INTO ledger_txn (account_id, posted_date, amount_cents, "
            "payee, category_id, is_split) "
            "VALUES ('a1','2026-08-01',-1000,'SHOP','c1',0)")
        lid = cur.lastrowid
    pt = insert_pending_txn(db, user_id="steven", ynab_txn_id=f"ledger:{lid}",
                            ynab_account_id="a1", payee="SHOP",
                            amount_cents=-1000, txn_date=date(2026, 8, 1),
                            memo="")
    close_stale_pending(db)
    with storage.connect(db) as con:
        row = con.execute("SELECT status, filed_by FROM pending_txn "
                          "WHERE id = ?", (pt,)).fetchone()
    assert row["status"] == "pending"
    assert row["filed_by"] is None


def test_missing_ledger_row_is_still_dismissed(tmp_path):
    """The undecidable branch survives: a pending row older than 7 days whose
    ledger txn was deleted by dedupe cleanup has nothing left to decide."""
    db = _setup(tmp_path)
    pt = insert_pending_txn(db, user_id="steven", ynab_txn_id="ledger:99999",
                            ynab_account_id="a1", payee="GONE",
                            amount_cents=-500, txn_date=date(2026, 1, 1),
                            memo="")
    close_stale_pending(db)
    with storage.connect(db) as con:
        row = con.execute("SELECT status, filed_by, chosen_category "
                          "FROM pending_txn WHERE id = ?", (pt,)).fetchone()
    assert row["status"] == "categorized"
    assert row["filed_by"] == "dismissed"
    assert row["chosen_category"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_close_stale_pending.py -v`
Expected: `test_categorized_ledger_no_longer_silently_closes` FAILS (status is
`categorized`); the second test passes already.

- [ ] **Step 3: Delete the adoption branch**

In `bot/ynab_full_sync.py`, delete the first `con.execute(...)` inside
`close_stale_pending` — the `UPDATE pending_txn SET status = 'categorized',
chosen_category = (SELECT ...)` statement spanning lines 561-579 — along with
its `n = cur.rowcount` assignment. Replace the docstring with:

```python
def close_stale_pending(db_path) -> int:
    """Close Inbox rows that have nothing left to decide.

    Until 2026-08-01 this also adopted whatever category happened to sit on
    the ledger row and closed the question silently -- 66 of 213 July
    outflows. That branch is gone: every row is now dispatched
    (bot/dispatch.py), so a categorized ledger row behind a pending question
    means the question is still owed an answer, not that it can be swallowed.

    What remains is the genuinely undecidable case: the ledger txn was
    deleted by dedupe/orphan cleanup, or became a linked transfer.
    filed_by='dismissed' + chosen_category NULL keeps these out of
    ynab_writer, which inner-joins chosen_category.
    """
    n = 0
    with storage.connect(db_path) as con:
```

Keep the remaining `cur = con.execute(...)` dismissal statement and its
`n += cur.rowcount` (change `n = cur.rowcount` to `n += cur.rowcount` if the
existing code assigns rather than accumulates).

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_close_stale_pending.py -v`
Expected: 2 passed.

- [ ] **Step 5: Queue newly-synced YNAB rows**

In `bot/ynab_full_sync.py`, find where `_upsert_txn` reports `action == "new"`
inside `run_full_sync` (near line 526). After the `new += 1` increment, add a
`pending_txn` insert for rows that need a decision:

```python
        if action == "new":
            new += 1
            # 2026-08-01: YNAB-sourced charges must reach Telegram too.
            # Skip anything that can't be a real spending decision:
            # transfers, split parents, split children, off-budget accounts,
            # and inflows.
            _maybe_enqueue_synced_txn(db_path, parent_id, local_acct)
```

Add the helper above `close_stale_pending`:

```python
def _maybe_enqueue_synced_txn(db_path, ledger_txn_id: int,
                              account_id: str) -> int | None:
    """Create a pending_txn for a newly-synced YNAB charge, unless it is a
    kind that never warrants a question."""
    with storage.connect(db_path) as con:
        row = con.execute(
            """SELECT l.id, l.payee, l.amount_cents, l.posted_date,
                      l.is_split, l.parent_txn_id, a.on_budget
               FROM ledger_txn l JOIN account a ON a.id = l.account_id
               WHERE l.id = ?""",
            (ledger_txn_id,),
        ).fetchone()
    if row is None:
        return None
    payee = row["payee"] or ""
    if (row["amount_cents"] >= 0 or row["is_split"] == 1
            or row["parent_txn_id"] is not None
            or row["on_budget"] != 1
            or payee.startswith("Transfer :")):
        return None
    return storage.insert_pending_txn(
        db_path, user_id="steven", ynab_txn_id=f"ledger:{ledger_txn_id}",
        ynab_account_id=account_id, payee=payee,
        amount_cents=row["amount_cents"], txn_date=row["posted_date"],
        memo="",
    )
```

- [ ] **Step 6: Test the enqueue filter**

Append to `tests/test_close_stale_pending.py`:

```python
from bot.ynab_full_sync import _maybe_enqueue_synced_txn


def _mk_ledger(db, **kw):
    cols = {"account_id": "a1", "posted_date": "2026-08-01",
            "amount_cents": -1000, "payee": "SHOP", "is_split": 0,
            "parent_txn_id": None}
    cols.update(kw)
    with storage.connect(db) as con:
        cur = con.execute(
            "INSERT INTO ledger_txn (account_id, posted_date, amount_cents, "
            "payee, is_split, parent_txn_id) VALUES (?,?,?,?,?,?)",
            (cols["account_id"], cols["posted_date"], cols["amount_cents"],
             cols["payee"], cols["is_split"], cols["parent_txn_id"]))
        return cur.lastrowid


def test_synced_outflow_is_enqueued(tmp_path):
    db = _setup(tmp_path)
    lid = _mk_ledger(db)
    assert _maybe_enqueue_synced_txn(db, lid, "a1") is not None


def test_transfer_split_inflow_and_offbudget_are_not_enqueued(tmp_path):
    db = _setup(tmp_path)
    with storage.connect(db) as con:
        con.execute("INSERT INTO account (id, name, type, on_budget) "
                    "VALUES ('a2','Marcus','tracking',0)")
    cases = [
        {"payee": "Transfer : Chase Amazon"},
        {"is_split": 1},
        {"parent_txn_id": 1},
        {"amount_cents": 5000},
        {"account_id": "a2"},
    ]
    for kw in cases:
        lid = _mk_ledger(db, **kw)
        acct = kw.get("account_id", "a1")
        assert _maybe_enqueue_synced_txn(db, lid, acct) is None, kw
```

Run: `./.venv/Scripts/python.exe -m pytest tests/test_close_stale_pending.py -v`
Expected: 4 passed.

- [ ] **Step 7: Adopt YNAB-set categories visibly**

A category Allison sets directly in the YNAB app still wins — YNAB is upstream
within the rolling 7-day sync window. But it must stop being silent. Add to
`bot/ynab_full_sync.py`, above `close_stale_pending`:

```python
def adopt_ynab_categories(db_path) -> int:
    """A pending question whose ledger row now carries a YNAB category is
    answered -- by YNAB. Stamp filed_by='ynab' so the FYI sweep announces it.

    This is the ONLY remaining path that closes a question without a human,
    and unlike the janitor branch it replaced, it is visible: every row it
    touches produces an FYI with a Correct button.
    """
    with storage.connect(db_path) as con:
        rows = con.execute(
            """SELECT pt.id AS pt_id, l.category_id
               FROM pending_txn pt
               JOIN ledger_txn l
                 ON ('ledger:' || l.id = pt.ynab_txn_id
                     OR l.ynab_txn_id = pt.ynab_txn_id)
               WHERE pt.status = 'pending'
                 AND l.category_id IS NOT NULL""",
        ).fetchall()
        for r in rows:
            con.execute(
                "UPDATE pending_txn SET status = 'categorized', "
                "chosen_category = ?, chosen_at = ?, filed_by = 'ynab' "
                "WHERE id = ?",
                (r["category_id"], storage._utcnow(), r["pt_id"]),
            )
    if rows:
        storage.audit(db_path, "ynab_category_adopted", {
            "count": len(rows), "pt_ids": [r["pt_id"] for r in rows],
        })
    return len(rows)
```

Call it from `run_full_sync` immediately before `close_stale_pending`
(line 531):

```python
    ynab_adopted = adopt_ynab_categories(db_path)
    pending_closed = close_stale_pending(db_path)
```

and add `"ynab_adopted": ynab_adopted,` to the `summary` dict.

**Ordering matters:** `adopt_ynab_categories` must run *after*
`_maybe_enqueue_synced_txn` has created rows for this sync pass, and *before*
`close_stale_pending`. A row created and adopted in the same pass is correct —
YNAB brought both the transaction and its category.

- [ ] **Step 8: Test the adoption path**

Append to `tests/test_close_stale_pending.py`:

```python
from bot.ynab_full_sync import adopt_ynab_categories


def test_ynab_category_is_adopted_and_marked_for_fyi(tmp_path):
    db = _setup(tmp_path)
    with storage.connect(db) as con:
        cur = con.execute(
            "INSERT INTO ledger_txn (account_id, posted_date, amount_cents, "
            "payee, category_id, is_split) "
            "VALUES ('a1','2026-08-01',-1000,'SHOP','c1',0)")
        lid = cur.lastrowid
    pt = insert_pending_txn(db, user_id="steven", ynab_txn_id=f"ledger:{lid}",
                            ynab_account_id="a1", payee="SHOP",
                            amount_cents=-1000, txn_date=date(2026, 8, 1),
                            memo="")
    assert adopt_ynab_categories(db) == 1
    with storage.connect(db) as con:
        row = con.execute("SELECT status, filed_by, chosen_category "
                          "FROM pending_txn WHERE id = ?", (pt,)).fetchone()
    assert row["status"] == "categorized"
    assert row["filed_by"] == "ynab"
    assert row["chosen_category"] == "c1"


def test_uncategorized_ledger_row_is_left_pending(tmp_path):
    db = _setup(tmp_path)
    with storage.connect(db) as con:
        cur = con.execute(
            "INSERT INTO ledger_txn (account_id, posted_date, amount_cents, "
            "payee, is_split) VALUES ('a1','2026-08-01',-1000,'SHOP',0)")
        lid = cur.lastrowid
    pt = insert_pending_txn(db, user_id="steven", ynab_txn_id=f"ledger:{lid}",
                            ynab_account_id="a1", payee="SHOP",
                            amount_cents=-1000, txn_date=date(2026, 8, 1),
                            memo="")
    assert adopt_ynab_categories(db) == 0
    with storage.connect(db) as con:
        assert con.execute("SELECT status FROM pending_txn WHERE id=?",
                           (pt,)).fetchone()["status"] == "pending"
```

**Note:** `test_categorized_ledger_no_longer_silently_closes` from Step 1
still passes — it calls `close_stale_pending` directly, never
`adopt_ynab_categories`. That separation is the point: the janitor no longer
adopts anything, and adoption is now an explicit, audited, FYI-producing step.

Run: `./.venv/Scripts/python.exe -m pytest tests/test_close_stale_pending.py -v`
Expected: 6 passed.

- [ ] **Step 9: Run the sync suites**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_split_sync.py tests/test_sync_local_only_category.py -q`
Expected: all pass.

- [ ] **Step 10: Commit**

```bash
git add bot/ynab_full_sync.py tests/test_close_stale_pending.py
git commit -m "fix(sync): stop the janitor swallowing questions; queue synced charges"
```

---

### Task 6: FYI delivery — `fyi_message` anchor and the FYI sweep

**Files:**
- Modify: `bot/storage.py` (SCHEMA — add `fyi_message`), `bot/group_chat.py` (add `_sweep_fyis`, call it from `group_ping_loop`)
- Test: `tests/test_fyi_sweep.py`

**Interfaces:**
- Consumes: `dispatch.dispatch_pending`
- Produces:
  - `group_chat._pending_fyis(db_path, limit: int) -> list[dict]`
  - `group_chat._fyi_text(rows: list[dict]) -> str`
  - `group_chat._sweep_fyis(app, settings, group_id) -> None`
  - table `fyi_message(id, pending_txn_id, tg_chat_id, message_id, sent_at)`

- [ ] **Step 1: Add the anchor table**

In `bot/storage.py` SCHEMA, after the `auto_rule` block from Task 1:

```sql
-- FYI messages (2026-08-01). A rule-filed charge is announced, not asked.
-- Deliberately NOT a `question` row: question rows drive the loose-reply
-- heuristic and the 7-day expiry sweep, and ~101 no-reply-expected rows a
-- month would degrade reply matching for the ~95 that do need answers.
-- This table exists so a reply-to on an FYI can still find its transaction.
CREATE TABLE IF NOT EXISTS fyi_message (
  id              INTEGER PRIMARY KEY,
  pending_txn_id  INTEGER NOT NULL REFERENCES pending_txn(id),
  tg_chat_id      INTEGER NOT NULL,
  message_id      INTEGER NOT NULL,
  sent_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_fyi_msg
  ON fyi_message(tg_chat_id, message_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_fyi_pt
  ON fyi_message(pending_txn_id);
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_fyi_sweep.py`:

```python
from datetime import date

from bot import group_chat, storage
from bot.storage import init_db, insert_pending_txn


def _setup(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    with storage.connect(db) as con:
        con.execute("INSERT INTO category_group (id, name) VALUES ('g','G')")
        con.execute("INSERT INTO category (id, group_id, name) "
                    "VALUES ('c-elec','g','Electric (24th)')")
        con.execute("INSERT INTO account (id, name, type, on_budget) "
                    "VALUES ('a1','Chk','checking',1)")
    return db


def _filed_row(db, payee="DUKE ENERGY", rule_id=1):
    with storage.connect(db) as con:
        cur = con.execute(
            "INSERT INTO ledger_txn (account_id, posted_date, amount_cents, "
            "payee, is_split) VALUES ('a1','2026-08-01',-18400,?,0)", (payee,))
        lid = cur.lastrowid
    pt = insert_pending_txn(db, user_id="steven", ynab_txn_id=f"ledger:{lid}",
                            ynab_account_id="a1", payee=payee,
                            amount_cents=-18400, txn_date=date(2026, 8, 1),
                            memo="")
    with storage.connect(db) as con:
        con.execute("UPDATE pending_txn SET status='categorized', "
                    "chosen_category='c-elec', filed_by=? WHERE id=?",
                    (f"rule:{rule_id}", pt))
    return pt


def test_pending_fyis_finds_rule_filed_rows(tmp_path):
    db = _setup(tmp_path)
    pt = _filed_row(db)
    rows = group_chat._pending_fyis(db, limit=10)
    assert [r["id"] for r in rows] == [pt]
    assert rows[0]["category_name"] == "Electric (24th)"


def test_human_filed_rows_are_not_fyis(tmp_path):
    db = _setup(tmp_path)
    pt = _filed_row(db)
    with storage.connect(db) as con:
        con.execute("UPDATE pending_txn SET filed_by='steven' WHERE id=?",
                    (pt,))
    assert group_chat._pending_fyis(db, limit=10) == []


def test_ynab_adopted_rows_are_also_fyis(tmp_path):
    """A category Allison set in the YNAB app files without asking, so it
    owes an FYI too — that path was silent before 2026-08-01."""
    db = _setup(tmp_path)
    pt = _filed_row(db)
    with storage.connect(db) as con:
        con.execute("UPDATE pending_txn SET filed_by='ynab' WHERE id=?", (pt,))
    assert [r["id"] for r in group_chat._pending_fyis(db, limit=10)] == [pt]


def test_already_announced_rows_are_not_resent(tmp_path):
    db = _setup(tmp_path)
    pt = _filed_row(db)
    with storage.connect(db) as con:
        con.execute("INSERT INTO fyi_message (pending_txn_id, tg_chat_id, "
                    "message_id) VALUES (?, -100, 5)", (pt,))
    assert group_chat._pending_fyis(db, limit=10) == []


def test_fyi_text_coalesces(tmp_path):
    db = _setup(tmp_path)
    _filed_row(db, "DUKE ENERGY")
    _filed_row(db, "SPECTRUM")
    rows = group_chat._pending_fyis(db, limit=10)
    text = group_chat._fyi_text(rows)
    assert "DUKE ENERGY" in text and "SPECTRUM" in text
    assert "Electric (24th)" in text
    # One message, not two -- and it must say how to correct.
    assert text.count("\n") >= 2
    assert "reply" in text.lower()


def test_fyi_never_creates_question_rows(tmp_path):
    """THE regression guard. question rows drive the loose-reply heuristic
    and the 7-day expiry sweep; FYIs must stay out of that table."""
    db = _setup(tmp_path)
    _filed_row(db)
    rows = group_chat._pending_fyis(db, limit=10)
    group_chat._record_fyi_sent(db, [r["id"] for r in rows],
                                tg_chat_id=-100, message_id=7)
    with storage.connect(db) as con:
        n = con.execute("SELECT COUNT(*) FROM question").fetchone()[0]
        f = con.execute("SELECT COUNT(*) FROM fyi_message").fetchone()[0]
    assert n == 0
    assert f == 1
```

- [ ] **Step 3: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_fyi_sweep.py -v`
Expected: FAIL — `AttributeError: module 'bot.group_chat' has no attribute '_pending_fyis'`.

- [ ] **Step 4: Implement the FYI sweep**

In `bot/group_chat.py`, add near `_MAX_PINGS_PER_SWEEP` (line 36):

```python
# FYIs announce rule-filed charges. Higher cap than questions: they need no
# reply, so there is no reason to drip them.
_MAX_FYIS_PER_SWEEP = 20
```

Then add these functions after `_ping_text` (which ends at line 278):

```python
def _pending_fyis(db_path, limit: int) -> list[dict]:
    """Rows filed without asking, that have not been announced yet.

    Two producers: a rule (filed_by 'rule:<id>') and YNAB adopting a
    category Allison set in the app (filed_by 'ynab'). Both file without a
    question, so both owe an FYI. A human filing ('steven'/'allison'), a
    dismissal, or a transfer never produces one -- the person already knows.
    """
    with storage.connect(db_path) as con:
        rows = con.execute(
            """SELECT pt.id, pt.payee, pt.amount_cents, pt.txn_date,
                      pt.filed_by, c.name AS category_name
               FROM pending_txn pt
               LEFT JOIN category c ON c.id = pt.chosen_category
               WHERE (pt.filed_by LIKE 'rule:%' OR pt.filed_by = 'ynab')
                 AND NOT EXISTS (
                   SELECT 1 FROM fyi_message f WHERE f.pending_txn_id = pt.id
                 )
               ORDER BY pt.id ASC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def _fyi_text(rows: list[dict]) -> str:
    """One coalesced message for everything filed since the last sweep."""
    if not rows:
        return ""
    head = ("🗂 Auto-filed by your rules:" if len(rows) > 1
            else "🗂 Auto-filed by a rule:")
    lines = [head]
    for r in rows:
        lines.append(
            f"• {_fmt_money(r['amount_cents'])} {r['payee']} "
            f"({r['txn_date']}) → {r['category_name'] or '?'}"
        )
    lines.append("Wrong? Reply to this message with the right category.")
    return "\n".join(lines)


def _record_fyi_sent(db_path, pt_ids: list[int], *, tg_chat_id: int,
                     message_id: int) -> None:
    """Anchor the sent message so a reply-to can find these transactions.

    Writes fyi_message, never question -- see the table's schema comment.
    """
    with storage.connect(db_path) as con:
        for pt_id in pt_ids:
            con.execute(
                "INSERT OR IGNORE INTO fyi_message "
                "(pending_txn_id, tg_chat_id, message_id) VALUES (?, ?, ?)",
                (pt_id, tg_chat_id, message_id),
            )


async def _sweep_fyis(app: Application, settings, group_id: int) -> None:
    """Announce rule-filed charges in one coalesced message."""
    db_path = settings.paths.database
    rows = _pending_fyis(db_path, _MAX_FYIS_PER_SWEEP)
    if not rows:
        return
    bot = app.bot_data["chat_to_bot"].get(group_id)
    if bot is None:
        return
    try:
        sent = await bot.send_message(chat_id=group_id, text=_fyi_text(rows))
    except Exception as e:  # noqa: BLE001
        log.warning("fyi sweep send failed: %s", e)
        return
    _record_fyi_sent(db_path, [r["id"] for r in rows],
                     tg_chat_id=group_id, message_id=sent.message_id)
    storage.audit(db_path, "fyi_sent", {
        "count": len(rows), "message_id": sent.message_id,
        "pt_ids": [r["id"] for r in rows],
    })
```

- [ ] **Step 5: Wire dispatch and the FYI sweep into the loop**

In `group_ping_loop` (line 119-128), replace the body of the `try` with:

```python
        try:
            await asyncio.sleep(_SWEEP_INTERVAL_S)
            if _in_quiet_hours(settings.telegram.quiet_hours):
                continue
            # Decide file-vs-ask before either sweep runs, so a rule-filed
            # row is announced rather than asked in the same tick.
            from bot import dispatch
            await asyncio.to_thread(dispatch.dispatch_pending,
                                    settings.paths.database)
            await _sweep_fyis(app, settings, group_id)
            await _sweep_once(app, settings, group_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("group_ping_loop iteration failed: %s", e)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_fyi_sweep.py -v`
Expected: 6 passed.

- [ ] **Step 7: Commit**

```bash
git add bot/storage.py bot/group_chat.py tests/test_fyi_sweep.py
git commit -m "feat(auto-sync): FYI sweep announces rule-filed charges"
```

---

### Task 7: Correcting an FYI

**Files:**
- Modify: `bot/group_chat.py` (`handle_group_message` reply-to resolution)
- Test: `tests/test_fyi_correction.py`

**Interfaces:**
- Consumes: `_resolve_category`, `_file_item`
- Produces: `group_chat._fyi_for_reply(db_path, tg_chat_id: int, message_id: int) -> list[dict]`, `group_chat.correct_fyi(db_path, pt_id: int, category_id: str, user: str) -> bool`

- [ ] **Step 1: Write the failing test**

Create `tests/test_fyi_correction.py`:

```python
from datetime import date

from bot import group_chat, storage
from bot.storage import init_db, insert_pending_txn


def _setup(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    with storage.connect(db) as con:
        con.execute("INSERT INTO category_group (id, name) VALUES ('g','G')")
        con.execute("INSERT INTO category (id, group_id, name) "
                    "VALUES ('c-elec','g','Electric (24th)')")
        con.execute("INSERT INTO category (id, group_id, name) "
                    "VALUES ('c-gas','g','Gas (20th)')")
        con.execute("INSERT INTO account (id, name, type, on_budget) "
                    "VALUES ('a1','Chk','checking',1)")
        cur = con.execute(
            "INSERT INTO ledger_txn (account_id, posted_date, amount_cents, "
            "payee, category_id, is_split) "
            "VALUES ('a1','2026-08-01',-18400,'DUKE ENERGY','c-elec',0)")
        lid = cur.lastrowid
    pt = insert_pending_txn(db, user_id="steven", ynab_txn_id=f"ledger:{lid}",
                            ynab_account_id="a1", payee="DUKE ENERGY",
                            amount_cents=-18400, txn_date=date(2026, 8, 1),
                            memo="")
    with storage.connect(db) as con:
        con.execute("UPDATE pending_txn SET status='categorized', "
                    "chosen_category='c-elec', filed_by='rule:1', "
                    "synced_to_ynab_at='2026-08-01' WHERE id=?", (pt,))
        con.execute("INSERT INTO fyi_message (pending_txn_id, tg_chat_id, "
                    "message_id) VALUES (?, -100, 42)", (pt,))
    return db, pt, lid


def test_reply_to_fyi_finds_its_transactions(tmp_path):
    db, pt, _ = _setup(tmp_path)
    got = group_chat._fyi_for_reply(db, -100, 42)
    assert [r["pending_txn_id"] for r in got] == [pt]


def test_reply_to_unknown_message_finds_nothing(tmp_path):
    db, _, _ = _setup(tmp_path)
    assert group_chat._fyi_for_reply(db, -100, 999) == []


def test_correction_refiles_and_requeues_for_ynab(tmp_path):
    db, pt, lid = _setup(tmp_path)
    assert group_chat.correct_fyi(db, pt, "c-gas", "steven") is True
    with storage.connect(db) as con:
        row = con.execute("SELECT chosen_category, filed_by, "
                          "synced_to_ynab_at FROM pending_txn WHERE id=?",
                          (pt,)).fetchone()
        led = con.execute("SELECT category_id FROM ledger_txn WHERE id=?",
                          (lid,)).fetchone()
    assert row["chosen_category"] == "c-gas"
    assert row["filed_by"] == "steven"
    # Must re-push: the rule already sent the wrong category to YNAB.
    assert row["synced_to_ynab_at"] is None
    assert led["category_id"] == "c-gas"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_fyi_correction.py -v`
Expected: FAIL — `_fyi_for_reply` does not exist.

- [ ] **Step 3: Implement lookup and correction**

In `bot/group_chat.py`, after `_record_fyi_sent`:

```python
def _fyi_for_reply(db_path, tg_chat_id: int, message_id: int) -> list[dict]:
    """Transactions announced in the FYI message being replied to."""
    with storage.connect(db_path) as con:
        rows = con.execute(
            """SELECT f.pending_txn_id, pt.payee, pt.amount_cents
               FROM fyi_message f
               JOIN pending_txn pt ON pt.id = f.pending_txn_id
               WHERE f.tg_chat_id = ? AND f.message_id = ?
               ORDER BY f.pending_txn_id ASC""",
            (tg_chat_id, message_id),
        ).fetchall()
    return [dict(r) for r in rows]


def correct_fyi(db_path, pt_id: int, category_id: str, user: str) -> bool:
    """Re-file a rule-filed transaction with a human's category.

    synced_to_ynab_at is cleared because the rule's (wrong) category may
    already have been pushed -- the writer must send the correction.
    """
    with storage.connect(db_path) as con:
        pt = con.execute(
            "SELECT id, ynab_txn_id FROM pending_txn WHERE id = ?", (pt_id,),
        ).fetchone()
        if pt is None:
            return False
        con.execute(
            "UPDATE pending_txn SET chosen_category = ?, chosen_at = ?, "
            "status = 'categorized', filed_by = ?, "
            "synced_to_ynab_at = NULL WHERE id = ?",
            (category_id, storage._utcnow(), user, pt_id),
        )
        yid = pt["ynab_txn_id"] or ""
        if yid.startswith("ledger:"):
            try:
                con.execute(
                    "UPDATE ledger_txn SET category_id = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (category_id, int(yid.split(":", 1)[1])),
                )
            except (ValueError, IndexError):
                pass
        elif yid:
            con.execute(
                "UPDATE ledger_txn SET category_id = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE ynab_txn_id = ?",
                (category_id, yid),
            )
    storage.audit(db_path, "fyi_corrected", {
        "pending_txn_id": pt_id, "category_id": category_id, "user": user,
    })
    return True
```

- [ ] **Step 4: Route replies in `handle_group_message`**

In `handle_group_message` (line 387), where an incoming message's
`reply_to_message` is resolved against the `question` table, add an FYI check
**before** falling through to the loose-reply heuristic. Insert after the
existing question lookup fails:

```python
    # A reply to an FYI corrects it. Checked after the question lookup so a
    # genuine open question always wins the same message id.
    if msg.reply_to_message is not None:
        fyis = _fyi_for_reply(db_path, msg.chat_id,
                              msg.reply_to_message.message_id)
        if fyis:
            cat = _resolve_category(db_path, (msg.text or "").strip())
            if cat is None:
                await msg.reply_text(
                    "I didn't recognize that category — reply with the "
                    "category name and I'll re-file it.")
                return
            target = fyis[0] if len(fyis) == 1 else None
            if target is None:
                listing = "\n".join(
                    f"{i+1}. {_fmt_money(f['amount_cents'])} {f['payee']}"
                    for i, f in enumerate(fyis))
                await msg.reply_text(
                    f"That message covered {len(fyis)} charges — which one?\n"
                    f"{listing}")
                return
            correct_fyi(db_path, target["pending_txn_id"], cat["id"], user)
            await msg.reply_text(
                f"Re-filed {target['payee']} → {cat['name']}.")
            return
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_fyi_correction.py -v`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add bot/group_chat.py tests/test_fyi_correction.py
git commit -m "feat(auto-sync): reply to an FYI to correct it"
```

---

### Task 8: Rule discovery

**Files:**
- Modify: `bot/dispatch.py` (add `suggest_rules`)
- Test: `tests/test_rule_discovery.py`

**Interfaces:**
- Consumes: `suggest.normalize_payee`, `payee_overrides.match_rule`
- Produces: `dispatch.suggest_rules(db_path, *, days: int = 365, min_count: int = 3, min_share: float = 0.70, limit: int = 50) -> list[dict]` — each item `{"payee_key", "sample_payee", "category_id", "category_name", "count", "total_count", "share", "avg_amount_cents", "last_seen"}`

- [ ] **Step 1: Write the failing test**

Create `tests/test_rule_discovery.py`:

```python
from bot import dispatch, storage
from bot.storage import init_db


def _setup(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    with storage.connect(db) as con:
        con.execute("INSERT INTO category_group (id, name) VALUES ('g','G')")
        con.execute("INSERT INTO category (id, group_id, name) "
                    "VALUES ('c-groc','g','Groceries')")
        con.execute("INSERT INTO category (id, group_id, name) "
                    "VALUES ('c-din','g','Dining Out/Entertainment')")
        con.execute("INSERT INTO account (id, name, type, on_budget) "
                    "VALUES ('a1','Chk','checking',1)")
        con.execute("INSERT INTO account (id, name, type, on_budget) "
                    "VALUES ('a2','Marcus','tracking',0)")
    return db


def _txn(db, payee, cat, cents=-5000, acct="a1", days_ago=10, is_split=0):
    with storage.connect(db) as con:
        con.execute(
            "INSERT INTO ledger_txn (account_id, posted_date, amount_cents, "
            "payee, category_id, is_split) "
            "VALUES (?, date('now', ?), ?, ?, ?, ?)",
            (acct, f"-{days_ago} days", cents, payee, cat, is_split))


def test_dominant_payee_is_suggested(tmp_path):
    db = _setup(tmp_path)
    for i in range(5):
        _txn(db, f"HARRIS TEETER #{i}", "c-groc")
    got = dispatch.suggest_rules(db)
    assert len(got) == 1
    assert got[0]["payee_key"] == "HARRIS TEETER"
    assert got[0]["category_id"] == "c-groc"
    assert got[0]["count"] == 5
    assert got[0]["share"] == 1.0


def test_below_min_count_is_not_suggested(tmp_path):
    db = _setup(tmp_path)
    _txn(db, "RARE SHOP", "c-groc")
    _txn(db, "RARE SHOP", "c-groc")
    assert dispatch.suggest_rules(db) == []


def test_split_vote_is_not_suggested(tmp_path):
    db = _setup(tmp_path)
    for _ in range(3):
        _txn(db, "AMBIGUOUS", "c-groc")
    for _ in range(3):
        _txn(db, "AMBIGUOUS", "c-din")
    assert dispatch.suggest_rules(db) == []


def test_payee_with_existing_rule_is_excluded(tmp_path):
    db = _setup(tmp_path)
    for i in range(5):
        _txn(db, f"HARRIS TEETER #{i}", "c-groc")
    storage.create_auto_rule(db, pattern=r"HARRIS\s*TEETER",
                             category_id="c-groc", created_by="steven")
    assert dispatch.suggest_rules(db) == []


def test_transfers_splits_inflows_offbudget_excluded(tmp_path):
    db = _setup(tmp_path)
    for _ in range(4):
        _txn(db, "Transfer : Chase Amazon", "c-groc")
        _txn(db, "SPLIT PARENT", "c-groc", is_split=1)
        _txn(db, "PAYCHECK", "c-groc", cents=5000)
        _txn(db, "OFFBUDGET SHOP", "c-groc", acct="a2")
    assert dispatch.suggest_rules(db) == []


def test_outside_window_excluded(tmp_path):
    db = _setup(tmp_path)
    for i in range(5):
        _txn(db, f"OLD SHOP #{i}", "c-groc", days_ago=800)
    assert dispatch.suggest_rules(db) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_rule_discovery.py -v`
Expected: FAIL — `dispatch` has no attribute `suggest_rules`.

- [ ] **Step 3: Implement `suggest_rules`**

Append to `bot/dispatch.py`:

```python
def suggest_rules(db_path: Path | str, *, days: int = 365,
                  min_count: int = 3, min_share: float = 0.70,
                  limit: int = 50) -> list[dict]:
    """Payees consistently filed to one category that have no rule yet.

    This is the auto_prior logic, repurposed. It used to file transactions
    silently; now it only proposes rules for the Auto-Sync panel and a human
    decides. Recomputed on every call -- no cache to go stale.

    Household counting rules apply: outflows only, no 'Transfer :' payees,
    no split parents or children, on-budget accounts only.
    """
    import collections

    from bot.suggest import normalize_payee

    with storage.connect(db_path) as con:
        rows = con.execute(
            """SELECT l.payee, l.amount_cents, l.posted_date,
                      l.category_id, c.name AS category_name
               FROM ledger_txn l
               JOIN category c ON c.id = l.category_id
               WHERE l.posted_date >= date('now', ?)
                 AND l.amount_cents < 0
                 AND l.is_split = 0
                 AND l.parent_txn_id IS NULL
                 AND COALESCE(l.payee, '') NOT LIKE 'Transfer :%'
                 AND l.account_id IN (
                   SELECT id FROM account WHERE on_budget = 1
                 )""",
            (f"-{days} days",),
        ).fetchall()

    groups: dict[str, list] = collections.defaultdict(list)
    for r in rows:
        key = normalize_payee(r["payee"])
        if key:
            groups[key].append(r)

    out: list[dict] = []
    for key, items in groups.items():
        total = len(items)
        if total < min_count:
            continue
        votes = collections.Counter(
            (i["category_id"], i["category_name"]) for i in items)
        (cat_id, cat_name), n = votes.most_common(1)[0]
        share = n / total
        if share < min_share:
            continue
        # A payee an existing rule already handles is not a suggestion.
        if payee_overrides.match_rule(db_path, items[0]["payee"]) is not None:
            continue
        matching = [i for i in items if i["category_id"] == cat_id]
        out.append({
            "payee_key": key,
            "sample_payee": items[0]["payee"],
            "category_id": cat_id,
            "category_name": cat_name,
            "count": n,
            "total_count": total,
            "share": round(share, 4),
            "avg_amount_cents": int(
                sum(i["amount_cents"] for i in matching) / len(matching)),
            "last_seen": max(str(i["posted_date"]) for i in items),
        })

    out.sort(key=lambda d: (-d["count"], d["payee_key"]))
    return out[:limit]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_rule_discovery.py -v`
Expected: 6 passed.

- [ ] **Step 5: Sanity-check against the live DB**

Run:
```bash
./.venv/Scripts/python.exe -c "
from bot import dispatch
for s in dispatch.suggest_rules('ynab_helper.db')[:12]:
    print(f\"{s['payee_key'][:32]:32} {s['count']:3}/{s['total_count']:<3} -> {s['category_name']}\")
"
```
Expected: Harris Teeter, Mass Mutual, Oak City Psychology, The Car Park, and
similar near the top — matching the spec's measured list.

- [ ] **Step 6: Commit**

```bash
git add bot/dispatch.py tests/test_rule_discovery.py
git commit -m "feat(auto-sync): rule discovery from spending history"
```

---

### Task 9: HTTP API endpoints

**Files:**
- Modify: `bot/http_api.py` (module-scope body models near the other models; routes after the `/reconciler/*` group at line 1126-1165)
- Test: `tests/test_rules_api.py`

**Interfaces:**
- Consumes: `storage.*_auto_rule`, `dispatch.suggest_rules`
- Produces: `GET /rules`, `POST /rules`, `PATCH /rules/{rule_id}`, `DELETE /rules/{rule_id}`, `GET /rules/suggestions`, `POST /rules/preview`

- [ ] **Step 1: Write the failing test**

Create `tests/test_rules_api.py`, following the pattern in
`tests/test_investments_api.py` (read it first for the app/client fixture
shape used in this codebase):

```python
import pytest
from fastapi.testclient import TestClient

from bot import storage
from bot.http_api import build_app
from bot.storage import init_db


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("YNABHELPER_API_TOKEN", raising=False)
    db = tmp_path / "t.db"
    init_db(db)
    with storage.connect(db) as con:
        con.execute("INSERT INTO category_group (id, name) VALUES ('g','G')")
        con.execute("INSERT INTO category (id, group_id, name) "
                    "VALUES ('c-groc','g','Groceries')")
        con.execute("INSERT INTO account (id, name, type, on_budget) "
                    "VALUES ('a1','Chk','checking',1)")
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    (token_dir / "ui_api_token.txt").write_text("testtoken", encoding="utf-8")
    app = build_app(db_path=db, token_dir=token_dir,
                    webui_dir=tmp_path / "webui")
    c = TestClient(app)
    c.headers.update({"X-API-Token": "testtoken"})
    c.db = db
    return c


def test_requires_token(client):
    """Auth is the X-API-Token header, not Authorization: Bearer."""
    bare = TestClient(client.app)
    assert bare.get("/rules").status_code in (401, 403, 422)


def test_create_and_list(client):
    r = client.post("/rules", json={
        "pattern": r"\bHARRIS\s*TEETER\b", "category_id": "c-groc",
        "note": "weekly"})
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    listed = client.get("/rules").json()
    assert len(listed) == 1
    assert listed[0]["id"] == rid
    assert listed[0]["category_name"] == "Groceries"


def test_duplicate_pattern_is_409(client):
    body = {"pattern": r"\bX\b", "category_id": "c-groc"}
    assert client.post("/rules", json=body).status_code == 200
    assert client.post("/rules", json=body).status_code == 409


def test_invalid_regex_is_400(client):
    r = client.post("/rules", json={
        "pattern": "[unclosed", "category_id": "c-groc"})
    assert r.status_code == 400


def test_patch_and_delete(client):
    rid = client.post("/rules", json={
        "pattern": r"\bY\b", "category_id": "c-groc"}).json()["id"]
    assert client.patch(f"/rules/{rid}",
                        json={"enabled": False}).status_code == 200
    assert client.get("/rules").json()[0]["enabled"] == 0
    assert client.delete(f"/rules/{rid}").status_code == 200
    assert client.get("/rules").json() == []


def test_preview_lists_matching_transactions(client):
    with storage.connect(client.db) as con:
        for i in range(3):
            con.execute(
                "INSERT INTO ledger_txn (account_id, posted_date, "
                "amount_cents, payee, is_split) VALUES "
                "('a1', date('now','-5 days'), -4200, ?, 0)",
                (f"HARRIS TEETER #{i}",))
    r = client.post("/rules/preview", json={"pattern": r"HARRIS\s*TEETER"})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 3
    assert len(body["transactions"]) == 3
    assert "HARRIS TEETER" in body["transactions"][0]["payee"]


def test_suggestions_endpoint(client):
    with storage.connect(client.db) as con:
        for i in range(4):
            con.execute(
                "INSERT INTO ledger_txn (account_id, posted_date, "
                "amount_cents, payee, category_id, is_split) VALUES "
                "('a1', date('now','-5 days'), -4200, ?, 'c-groc', 0)",
                (f"HARRIS TEETER #{i}",))
    got = client.get("/rules/suggestions").json()
    assert got[0]["payee_key"] == "HARRIS TEETER"
    assert got[0]["category_name"] == "Groceries"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_rules_api.py -v`
Expected: FAIL — 404 on every route.

- [ ] **Step 3: Add module-scope body models**

In `bot/http_api.py`, at **module scope** (never inside `build_app`), next to
the other Pydantic models:

```python
class RuleCreateBody(BaseModel):
    pattern: str
    category_id: str
    note: str | None = None


class RulePatchBody(BaseModel):
    pattern: str | None = None
    category_id: str | None = None
    enabled: bool | None = None
    note: str | None = None


class RulePreviewBody(BaseModel):
    pattern: str
    days: int = 90
```

- [ ] **Step 4: Add the routes**

Inside `build_app`, after the `/reconciler/*` routes (line ~1165):

```python
    @app.get("/rules", dependencies=[Depends(_require_token)])
    def list_rules():
        rules = storage.list_auto_rules(db_path)
        with storage.connect(db_path) as con:
            names = {r["id"]: r["name"]
                     for r in con.execute("SELECT id, name FROM category")}
        for r in rules:
            r["category_name"] = names.get(r["category_id"])
        return rules

    @app.post("/rules")
    def create_rule(body: RuleCreateBody,
                    user: str = Depends(_require_token)):
        # _require_token RETURNS the user string (http_api.py:315-316), so
        # this route takes it as a parameter instead of using the
        # dependencies=[] form — same pattern as /categorize at line 555.
        try:
            re.compile(body.pattern)
        except re.error as e:
            raise HTTPException(400, f"invalid pattern: {e}")
        try:
            rid = storage.create_auto_rule(
                db_path, pattern=body.pattern, category_id=body.category_id,
                created_by=user, note=body.note,
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, "a rule with that pattern already exists")
        storage.audit(db_path, "rule_created", {
            "rule_id": rid, "pattern": body.pattern,
            "category_id": body.category_id,
        })
        return {"id": rid}

    @app.patch("/rules/{rule_id}", dependencies=[Depends(_require_token)])
    def patch_rule(rule_id: int, body: RulePatchBody):
        if body.pattern is not None:
            try:
                re.compile(body.pattern)
            except re.error as e:
                raise HTTPException(400, f"invalid pattern: {e}")
        try:
            ok = storage.update_auto_rule(
                db_path, rule_id, pattern=body.pattern,
                category_id=body.category_id, enabled=body.enabled,
                note=body.note,
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, "a rule with that pattern already exists")
        if not ok:
            raise HTTPException(404, "unknown rule")
        storage.audit(db_path, "rule_updated", {"rule_id": rule_id})
        return {"ok": True}

    @app.delete("/rules/{rule_id}", dependencies=[Depends(_require_token)])
    def delete_rule(rule_id: int):
        if not storage.delete_auto_rule(db_path, rule_id):
            raise HTTPException(404, "unknown rule")
        storage.audit(db_path, "rule_deleted", {"rule_id": rule_id})
        return {"ok": True}

    @app.get("/rules/suggestions", dependencies=[Depends(_require_token)])
    def rule_suggestions():
        from bot import dispatch
        return dispatch.suggest_rules(db_path)

    @app.post("/rules/preview", dependencies=[Depends(_require_token)])
    def preview_rule(body: RulePreviewBody):
        """Transactions this pattern WOULD have matched. The panel shows
        these before saving so the blast radius is visible."""
        try:
            pat = re.compile(body.pattern, re.I)
        except re.error as e:
            raise HTTPException(400, f"invalid pattern: {e}")
        with storage.connect(db_path) as con:
            rows = con.execute(
                """SELECT l.id, l.posted_date, l.payee, l.amount_cents,
                          c.name AS category_name
                   FROM ledger_txn l
                   LEFT JOIN category c ON c.id = l.category_id
                   WHERE l.posted_date >= date('now', ?)
                     AND l.amount_cents < 0
                     AND l.is_split = 0
                     AND l.parent_txn_id IS NULL
                     AND COALESCE(l.payee, '') NOT LIKE 'Transfer :%'
                     AND l.account_id IN (
                       SELECT id FROM account WHERE on_budget = 1
                     )
                   ORDER BY l.posted_date DESC""",
                (f"-{max(1, min(body.days, 730))} days",),
            ).fetchall()
        hits = [dict(r) for r in rows if pat.search(r["payee"] or "")]
        return {"count": len(hits), "transactions": hits[:200]}
```

Ensure `import re` and `import sqlite3` are present at the top of
`bot/http_api.py`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_rules_api.py -v`
Expected: 7 passed.

- [ ] **Step 6: Verify no route regressions**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_investments_api.py tests/test_webui_queries.py -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add bot/http_api.py tests/test_rules_api.py
git commit -m "feat(auto-sync): /rules CRUD, suggestions, and preview endpoints"
```

---

### Task 10: Auto-Sync panel UI

**Files:**
- Create: `../ynabhelper-ui/src/pages/AutoSync.tsx`
- Modify: `../ynabhelper-ui/src/App.tsx` (import + route + sidebar entry under Core)
- Reference: `../ynabhelper-ui/src/pages/Reconciler.tsx` for the page shell, `BotControl.tsx` for the `title=` prop convention

**Interfaces:**
- Consumes: `GET /rules`, `POST /rules`, `PATCH /rules/{id}`, `DELETE /rules/{id}`, `GET /rules/suggestions`, `POST /rules/preview`
- Produces: route `/auto-sync`, default export `AutoSyncPage`

> **Why this task is specified at the interface level, not line-by-line.**
> Every other task in this plan carries literal code because I read the file
> it modifies. This one lives in the sibling `ynabhelper-ui` repo, whose
> component shell, fetch wrapper, and styling conventions I have not read.
> Inventing ~400 lines of TSX against guessed conventions would produce code
> that compiles and looks wrong. Step 1 is therefore mandatory, not advisory:
> read `Reconciler.tsx` first and copy its structure. The API contract above
> is exact and fully tested by Task 9, so the boundary is unambiguous even
> though the rendering is not spelled out.

- [ ] **Step 1: Read the existing page conventions**

Read `../ynabhelper-ui/src/pages/Reconciler.tsx` in full — it is the closest
analogue (tabbed, read-mostly, API-driven). Match its data-fetching helper,
loading/error states, page shell component, and `title=` prop. Do not
introduce a new fetch wrapper or state library.

- [ ] **Step 2: Build the Rules tab**

Create `../ynabhelper-ui/src/pages/AutoSync.tsx` with two tabs, `Rules` and
`Suggestions`, using the same tab mechanism as `Reconciler.tsx`.

Rules tab: a table sorted by `fire_count` **descending** for display (matching
is id-ordered on the server; this is presentation only). Columns: pattern,
category, `fired N times`, `last fired`, enabled toggle, edit, delete. Rows
whose `last_fired_at` is null or older than 6 months render de-emphasized
(reduced opacity) with a `not firing` marker.

Per the standing UI rule, show outcomes and transactions — never confidence
scores, weights, or vote shares.

- [ ] **Step 3: Build the Suggestions tab**

A list from `GET /rules/suggestions`. Each row shows payee, proposed category,
`filed this way N of M times`, average amount, last seen, and a
`Create rule` button. `N of M` is a plain count of past filings, not a
confidence score — it is the evidence, stated as transactions.

- [ ] **Step 4: Build the preview-before-save dialog**

Creating or editing a rule opens a dialog that calls `POST /rules/preview`
with the current pattern and renders the returned transactions — date, payee,
amount, current category. Header reads
`This rule would have matched N transactions in the last 90 days`.

The Save button is disabled until a preview has been fetched for the current
pattern. This is the safety feature: the blast radius must be visible before a
rule can file anything.

Surface a 409 from `POST /rules` as `A rule with that pattern already exists`
and a 400 as the regex error text.

- [ ] **Step 5: Register the route**

In `../ynabhelper-ui/src/App.tsx`:

```tsx
import AutoSyncPage from "@/pages/AutoSync";
```

and alongside the other routes (near line 186):

```tsx
<Route path="/auto-sync" element={<AutoSyncPage />} />
```

Add the sidebar entry under the **Core** group, next to Reconciler and Bot
Control, following whatever nav-item structure that file already uses.

- [ ] **Step 6: Typecheck and build**

Run: `cd ../ynabhelper-ui && npm run build`
Expected: clean build, no TypeScript errors.

- [ ] **Step 7: Verify against the running bot**

With the bot running (scheduled task — do not start one yourself), run
`npm run tauri dev`, open Core → Auto-Sync, and confirm:
- Rules tab lists the 40 seeded rules.
- Suggestions tab shows Harris Teeter, Mass Mutual, and the rest.
- Creating a rule from a suggestion shows the preview, saves, and the new rule
  appears in the Rules tab.

- [ ] **Step 8: Commit both repos**

```bash
cd ../ynabhelper-ui
git add src/pages/AutoSync.tsx src/App.tsx
git commit -m "feat: Auto-Sync panel for payee rules and discovery"
```

---

### Task 11: Full verification

- [ ] **Step 1: Run the whole suite**

Run: `./.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: all pass. Record the count.

- [ ] **Step 2: Suggestion-quality regression check**

Run: `./.venv/Scripts/python.exe scripts/eval_suggestions.py`
Expected: accuracy at or above the pre-change baseline. Suggestions now
pre-fill the buttons on ~95 questions/month, so a regression here is a real
cost. If it dropped, stop and investigate before deploying.

- [ ] **Step 3: Dry-run the dispatcher against a DB copy**

Never run this against the live DB first — bulk operations on a copy, then
compare.

```bash
cp ynab_helper.db /c/Users/Steven/AppData/Local/Temp/claude/dispatch-test.db
./.venv/Scripts/python.exe -c "
from bot import dispatch
print(dispatch.dispatch_pending('/c/Users/Steven/AppData/Local/Temp/claude/dispatch-test.db'))
"
```
Expected: a `{'filed': N, 'asked': M}` dict. Confirm N is roughly the count of
rows matching the 40 seeded rules, not the whole backlog.

- [ ] **Step 4: Confirm no Amazon files were touched**

Run: `git diff --stat master -- bot/ | grep -i amazon`
Expected: no output. If `bot/amazon_tracker.py` or the Amazon bucket logic in
`bot/ingest.py` appears, revert those hunks — another effort owns that code.

- [ ] **Step 5: Commit any fixes and report**

Report to Steven: test count, `eval_suggestions.py` before/after numbers, the
dispatcher dry-run counts, and confirmation that the bot must be restarted
(the `YNAB-Helper-Bot` scheduled task) for any of this to go live.

---

## Deployment notes

- Code goes live only when the `YNAB-Helper-Bot` scheduled task restarts. Do
  not start a bot from a session — the scheduled task owns lifecycle, and an
  in-session bot causes port conflicts on `:8765`.
- The Tauri app ships via `npm run tauri build` then hot-swapping the exe over
  the install dir at `AppData\Local`. No reinstall needed.
- The mobile web bundle deploys with `deploy_webui.ps1` and needs no restart.
- Run `scripts/seed_auto_rules.py` exactly once against the live DB (Task 2
  Step 7). It is idempotent on pattern collision, but verify the count is 40
  rather than running it repeatedly.
