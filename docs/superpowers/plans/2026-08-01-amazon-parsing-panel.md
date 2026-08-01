# Amazon Parsing Panel + Drain Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the three per-person Amazon buckets with a single `Amazon Uncategorized` holding category the household actively drains, persist every charge↔order match so the matching process becomes visible, and promote the existing read-only Amazon view into a working panel with drain and manual-match actions.

**Architecture:** The bot's matcher already performs at the ceiling of what receipt capture allows (94% in July, 16 of 16 available receipts, matched at ingest). **Its rules are not changed.** What is missing is that the match is never written down — `_enrich_from_pending_order` scores a match, uses it, and discards it. Adding `ledger_txn.pending_order_id` makes "matched" queryable, which is the foundation for the panel, the pings, and any future diagnosis. A duplicate matcher in Rust (`q_amazon_reconcile`) is deleted so the panel reports what the bot actually did rather than what a second set of rules would have done.

**Tech Stack:** Python 3 + FastAPI + SQLite (bot repo `C:\Users\Steven\ynabhelper`), python-telegram-bot, React 18 + TypeScript + TanStack Query + Rust/Tauri 2 (UI repo `C:\Users\Steven\ynabhelper-ui`).

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-08-01-amazon-parsing-panel-design.md`. Read it before Task 1.
- **Line numbers in this plan are indicative, not authoritative.** A parallel agent is actively editing `bot/ingest.py`, `bot/telegram_bot.py`, and `bot/http_api.py` on this branch (the auto-sync / `bot/dispatch.py` work, commits `338909e`…`90910de`). **Locate every edit by grepping for the named symbol**, not by line number. If a target symbol has already been removed or renamed by that work, stop and ask rather than guessing.
- **Known collision:** that agent's `90910de` ("stop auto-filing; queue ynab_sync rows") restructured the same ingest region Task 4 rewrites, and its code references `is_large_amazon`, which Task 5 deletes. Before starting Phase 2, re-read the current Amazon fork and confirm with Steven that both designs still agree on what happens to an Amazon charge at ingest.
- **Never PID-kill or start the bot.** It is the `YNAB-Helper-Bot` scheduled task and owns its own lifecycle; a parallel agent may be deploying. Bot code goes live only on its restart. Use the Bot Control panel if it must be cycled.
- **Never bulk-recompute `month_category`.** Measured at +$10.5k phantom Ready-to-Assign. Use `envelope.apply_activity_delta` for a targeted fix; never `recompute_month` over history.
- **The matcher's rules in `bot/matcher.py` are not modified by this plan.** No threshold, weight, window, or tolerance changes. If a task seems to require one, stop and ask.
- **Pydantic body models live at module scope**, never inside `build_app()` — a closure-scoped model makes every POST return 422.
- **New FastAPI routes must be declared above the catch-all** `GET /{full_path:path}` or they return `index.html` instead of JSON.
- **Bank records outrank YNAB.** Nothing in this plan pushes to YNAB.
- **UI repo has no test harness** (no vitest, no test files). UI verification is `npm run build` (`tsc -b && vite build`) plus the scripted manual pass in Task 12.
- **Python tests:** `pytest tests/ -v` from the bot repo root, using `.venv/Scripts/python.exe -m pytest` on this machine (`python` is not on PATH).
- **PowerShell scripts need** `Set-ExecutionPolicy -Scope Process Bypass -Force`.
- **The UI has three data-source modes** (`src/lib/db.ts`): Tauri (Rust `q_*`), HTTP (`POST /q/{name}` → `bot/webui_queries.py`), and mock (`src/lib/mockData.ts`). A query changed in one must be changed in all three.
- **Rust changes require a full `npm run tauri build`** — a bundle hot-swap will not pick them up.
- **UI shows outcomes, not internals.** `match_score` is persisted for diagnosis and must never be rendered.

---

## File Structure

### Bot repo (`C:\Users\Steven\ynabhelper`)

| File | Responsibility |
|---|---|
| `bot/storage.py` | **Modify** `_migrate()` — add `ledger_txn.pending_order_id`, `match_score`, index. |
| `bot/ingest.py` | **Modify** — persist the match; collapse the Amazon fork to one path; delete the large-charge machinery. |
| `bot/queue_lane.py` | **Modify** — delete the Amazon HOLD/TTL branches. |
| `bot/amazon_tracker.py` | **Delete.** |
| `bot/batch_processor.py` | **Modify** — delete `build_amazon_batch`, `count_amazon_ready`. |
| `bot/telegram_bot.py` | **Modify** — delete `/amazon` and the Amazon batch branch; add the two pings. |
| `bot/amazon_notify.py` | **Create** — the matched ping and the 4-day unmatched sweep. Kept out of `telegram_bot.py`, which is already ~2400 lines. |
| `bot/http_api.py` | **Modify** — add `POST /amazon/match`, `POST /amazon/unmatch`. |
| `bot/webui_queries.py` | **Modify** — `q_amazon_reconcile` for HTTP mode. |
| `scripts/backfill_amazon_matches.py` | **Create.** |
| `scripts/retire_amazon_buckets.py` | **Create.** |
| `tests/test_amazon_matching.py` | **Create.** |
| `tests/test_large_amazon.py` | **Delete** with the code it covers. |

### UI repo (`C:\Users\Steven\ynabhelper-ui`)

| File | Responsibility |
|---|---|
| `src-tauri/src/commands.rs` | **Modify** `q_amazon_reconcile` — delete the pairing logic, read the persisted link, add `who`. |
| `src/lib/types.ts` | **Modify** — `who` on `AmazonCharge`; `months` on `AmazonReconcile`. |
| `src/lib/mockData.ts` | **Modify** — mock fixture for the new shape. |
| `src/lib/api.ts` | **Modify** — `amazonMatch`, `amazonUnmatch`. |
| `src/pages/Amazon.tsx` | **Create** — the promoted panel. |
| `src/pages/Reconciler.tsx` | **Modify** — remove the Amazon tab and its components. |
| `src/pages/Transactions.tsx` | **Modify** — repoint the deep link to `/amazon?charge=`. |
| `src/App.tsx` | **Modify** — route + nav entry. |

---

# Phase 1 — Persist the match

### Task 1: Schema

**Files:**
- Modify: `bot/storage.py` (inside `_migrate()`, after the existing `pending_order` block near `:542`)
- Test: `tests/test_amazon_matching.py`

**Interfaces:**
- Produces: `ledger_txn.pending_order_id INTEGER`, `ledger_txn.match_score REAL`, index `ix_ledger_txn_pending_order`. Every later task reads these names.

- [ ] **Step 1: Write the failing test**

Create `tests/test_amazon_matching.py`:

```python
"""Charge<->order matches are persisted, and Amazon has one filing path.

Spec: docs/superpowers/specs/2026-08-01-amazon-parsing-panel-design.md
"""
from bot import storage


def test_ledger_txn_has_match_columns(tmp_path):
    db = tmp_path / "test.db"
    storage.init_db(db)
    with storage.connect(db) as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info(ledger_txn)")}
    assert "pending_order_id" in cols
    assert "match_score" in cols


def test_match_index_exists(tmp_path):
    db = tmp_path / "test.db"
    storage.init_db(db)
    with storage.connect(db) as con:
        idx = {r[1] for r in con.execute("PRAGMA index_list(ledger_txn)")}
    assert "ix_ledger_txn_pending_order" in idx


def test_migrate_is_idempotent(tmp_path):
    """_migrate runs on every init_db; a second run must not raise."""
    db = tmp_path / "test.db"
    storage.init_db(db)
    storage.init_db(db)
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_amazon_matching.py -v`
Expected: FAIL — `assert 'pending_order_id' in cols`.

- [ ] **Step 3: Add the migration**

In `bot/storage.py`, inside `_migrate()`, after the existing `pending_order`/`last_pushed_at` block:

```python
    # 2026-08-01: persist the charge<->order match. _enrich_from_pending_order
    # scored a match and threw it away, so "matched" was not a queryable
    # state and the matching process could not be diagnosed. Nullable and
    # additive; many charges may point at one order (split shipments).
    ledger_cols = {r[1] for r in con.execute("PRAGMA table_info(ledger_txn)")}
    if "pending_order_id" not in ledger_cols:
        con.execute("ALTER TABLE ledger_txn ADD COLUMN pending_order_id INTEGER")
    if "match_score" not in ledger_cols:
        con.execute("ALTER TABLE ledger_txn ADD COLUMN match_score REAL")
    # Index created here, not in SCHEMA: executescript(SCHEMA) runs before
    # this ALTER, so a SCHEMA-level index would fail with "no such column".
    con.execute(
        "CREATE INDEX IF NOT EXISTS ix_ledger_txn_pending_order "
        "ON ledger_txn(pending_order_id)"
    )
```

- [ ] **Step 4: Run it and watch it pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_amazon_matching.py -v`
Expected: 3 passed.

- [ ] **Step 5: Confirm nothing else broke**

Run: `.venv/Scripts/python.exe -m pytest tests/ -v`
Expected: all pass (`test_large_amazon.py` still passes — it is deleted in Task 5, not yet).

- [ ] **Step 6: Commit**

```bash
git add bot/storage.py tests/test_amazon_matching.py
git commit -m "feat(schema): persist the charge<->order match on ledger_txn"
```

---

### Task 2: Write the link at ingest and on the retro sweep

**Files:**
- Modify: `bot/ingest.py` (`_enrich_from_pending_order` near `:860`, its caller at `:211`, `retro_bucket_amazon_order` near `:991`)
- Test: `tests/test_amazon_matching.py`

**Interfaces:**
- Consumes: the columns from Task 1.
- Produces: `_enrich_from_pending_order(db_path, parsed, payee) -> dict | None` — **unchanged signature**, but the returned dict now carries an added key `_match_score: float`. The caller writes `pending_order_id` and `match_score` onto the `ledger_txn` row and flips `pending_order.status` to `'matched'`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_amazon_matching.py`:

```python
from datetime import date
from bot import ingest

ACCT = "acct-chase-1111"


def _setup(tmp_path):
    """Fresh DB with one CC account and a real spending category."""
    db = tmp_path / "test.db"
    storage.init_db(db)
    with storage.connect(db) as con:
        con.execute(
            "INSERT INTO account (id, name, type, on_budget, closed, last4) "
            "VALUES (?, 'Chase Amazon', 'credit_card', 1, 0, '1111')",
            (ACCT,),
        )
        con.execute(
            "INSERT INTO category_group (id, name) VALUES ('g1', 'Day to Day Expenses')"
        )
        for cid, name, is_spending in [
            ("cat-amz-unc", "Amazon Uncategorized", 0),
            ("cat-household", "Household Items", 1),
        ]:
            con.execute(
                "INSERT INTO category (id, group_id, name, hidden, is_spending) "
                "VALUES (?, 'g1', ?, 0, ?)",
                (cid, name, is_spending),
            )
    return db


def _insert_order(db, *, order_id, total_cents, order_date, person="steven"):
    with storage.connect(db) as con:
        con.execute(
            "INSERT INTO pending_order (id, user_id, source, external_id, "
            "  email_id, order_date, total_cents, raw_summary, status, "
            "  assigned_to_user_id) "
            "VALUES (?, 'steven', 'amazon', ?, ?, ?, ?, "
            "        '2 item(s): Dish Soap, Sponges', 'pending', ?)",
            (order_id, f"111-{order_id}", f"eml-{order_id}", order_date,
             total_cents, person),
        )


def test_ingest_persists_the_match(tmp_path):
    db = _setup(tmp_path)
    _insert_order(db, order_id=1, total_cents=4289, order_date="2026-07-10")
    ingest.ingest_signal(
        db,
        parsed={
            "kind": "cc_alert", "payee": "AMAZON MKTPLACE PMTS",
            "amount_cents": -4289, "posted_date": date(2026, 7, 12),
            "last4": "1111", "summary": "AMAZON MKTPLACE PMTS",
        },
        user_id="steven",
    )
    with storage.connect(db) as con:
        row = con.execute(
            "SELECT pending_order_id, match_score FROM ledger_txn"
        ).fetchone()
        order = con.execute(
            "SELECT status FROM pending_order WHERE id = 1"
        ).fetchone()
    assert row["pending_order_id"] == 1
    assert row["match_score"] is not None and row["match_score"] > 0
    assert order["status"] == "matched"


def test_ingest_with_no_order_leaves_link_null(tmp_path):
    db = _setup(tmp_path)
    ingest.ingest_signal(
        db,
        parsed={
            "kind": "cc_alert", "payee": "AMAZON MKTPLACE PMTS",
            "amount_cents": -4289, "posted_date": date(2026, 7, 12),
            "last4": "1111", "summary": "AMAZON MKTPLACE PMTS",
        },
        user_id="steven",
    )
    with storage.connect(db) as con:
        row = con.execute(
            "SELECT pending_order_id, match_score FROM ledger_txn"
        ).fetchone()
    assert row["pending_order_id"] is None
    assert row["match_score"] is None
```

`ingest_signal`'s exact keyword signature must be read from `bot/ingest.py` before running — adapt the two calls above to it rather than changing the production signature.

- [ ] **Step 2: Run and watch them fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_amazon_matching.py -v`
Expected: `test_ingest_persists_the_match` FAILS (`pending_order_id` is None); `test_ingest_with_no_order_leaves_link_null` passes vacuously.

- [ ] **Step 3: Return the score from the scorer**

In `bot/ingest.py`, at the end of `_enrich_from_pending_order`, replace `return best` with:

```python
    # Carry the score out so the caller can persist it. Keyed with a leading
    # underscore because `best` is a raw pending_order row dict and this is
    # not a column.
    best = dict(best)
    best["_match_score"] = match_score(best, txn_for_score, source=source)
    return best
```

and add `match_score` to the existing import at the top of the function:

```python
    from bot.matcher import _PAYEE_PATTERNS, find_best_match, match_score
```

- [ ] **Step 4: Persist it at the caller**

In `bot/ingest.py:212`, extend the existing `if matched_order:` block:

```python
            if matched_order:
                storage.audit(db_path, "ingest_enriched_from_order", {
                    "matched_order_id": matched_order.get("id"),
                    "source": matched_order.get("source"),
                    "had_chosen_category": bool(matched_order.get("chosen_category")),
                })
```

The `ledger_txn` row is inserted further down at `:279`. Add the two columns to
that INSERT, sourcing them from `matched_order`, and immediately after the
insert mark the order matched:

```python
            if matched_order:
                con.execute(
                    "UPDATE pending_order SET status = 'matched', "
                    "  updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (matched_order["id"],),
                )
```

Read the existing INSERT before editing and add `pending_order_id` /
`match_score` to its column list and parameter tuple, defaulting to `None` when
`matched_order` is falsy. Do not restructure the insert.

- [ ] **Step 5: Run and watch them pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_amazon_matching.py -v`
Expected: all pass.

- [ ] **Step 6: Write the retro-sweep test**

```python
def test_retro_sweep_links_a_late_order(tmp_path):
    """The order email arrives after the charge — the sweep must link it."""
    db = _setup(tmp_path)
    ingest.ingest_signal(
        db,
        parsed={
            "kind": "cc_alert", "payee": "AMAZON MKTPLACE PMTS",
            "amount_cents": -4289, "posted_date": date(2026, 7, 12),
            "last4": "1111", "summary": "AMAZON MKTPLACE PMTS",
        },
        user_id="steven",
    )
    _insert_order(db, order_id=7, total_cents=4289, order_date="2026-07-10")
    assert ingest.retro_bucket_amazon_order(db, order_id=7) is True
    with storage.connect(db) as con:
        row = con.execute(
            "SELECT pending_order_id FROM ledger_txn"
        ).fetchone()
    assert row["pending_order_id"] == 7
```

- [ ] **Step 7: Run it, watch it fail, then make `retro_bucket_amazon_order` write the link**

Run: `.venv/Scripts/python.exe -m pytest tests/test_amazon_matching.py::test_retro_sweep_links_a_late_order -v`
Expected: FAIL.

In `retro_bucket_amazon_order`, wherever it currently updates the matched
charge, add `pending_order_id = ?` and `match_score = ?` to that UPDATE and set
`pending_order.status = 'matched'`. Leave its candidate-selection logic
untouched — it is deliberately conservative (exactly one candidate).

- [ ] **Step 8: Run the full suite**

Run: `.venv/Scripts/python.exe -m pytest tests/ -v`
Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add bot/ingest.py tests/test_amazon_matching.py
git commit -m "feat(amazon): persist the match at ingest and on the retro sweep"
```

---

### Task 3: Backfill historical matches

**Files:**
- Create: `scripts/backfill_amazon_matches.py`

**Interfaces:**
- Consumes: Task 1 columns, `bot.matcher.find_best_match`, `bot.matcher.match_score`.
- Produces: a CLI. `--dry-run` is the default; `--apply` commits. Writes only `pending_order_id`, `match_score`, `pending_order.status`.

- [ ] **Step 1: Write the script**

```python
"""Backfill ledger_txn.pending_order_id for historical Amazon charges.

Re-runs the SAME matcher the bot uses (bot.matcher.find_best_match) over
charges that predate match persistence. Writes ONLY the link columns and
pending_order.status — never a category, never month_category, never YNAB.

Measured 2026-08-01: 44 of 103 charges since January match. History is
poor because of the multi-order parser bug (fixed 2026-07-26) and the
IMAP capture outage, not because of the matching rules. July, the first
clean month, matches 94% — the receipt-capture ceiling.

Usage:
    .venv/Scripts/python.exe scripts/backfill_amazon_matches.py
    .venv/Scripts/python.exe scripts/backfill_amazon_matches.py --apply
"""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from bot import storage
from bot.matcher import find_best_match, match_score

DB = Path("ynab_helper.db")

CHARGES_SQL = """
    SELECT id, posted_date, amount_cents, payee, memo
    FROM ledger_txn
    WHERE (UPPER(payee) LIKE '%AMAZON%' OR UPPER(payee) LIKE '%AMZN%')
      AND payee NOT LIKE 'Transfer :%'
      AND transfer_account_id IS NULL
      AND parent_txn_id IS NULL
      AND amount_cents < 0
      AND pending_order_id IS NULL
    ORDER BY posted_date
"""

ORDERS_SQL = """
    SELECT id, order_date, total_cents, external_id
    FROM pending_order
    WHERE source = 'amazon' AND total_cents > 0
"""


def _d(value) -> date:
    y, m, d = str(value)[:10].split("-")
    return date(int(y), int(m), int(d))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="commit the links (default is a dry run)")
    ap.add_argument("--db", default=str(DB))
    args = ap.parse_args()

    with storage.connect(args.db) as con:
        charges = [dict(r) for r in con.execute(CHARGES_SQL)]
        orders = [dict(r) for r in con.execute(ORDERS_SQL)]
    for o in orders:
        o["order_date"] = _d(o["order_date"])

    links: list[tuple[int, int, float]] = []
    for c in charges:
        txn = {
            "amount_cents": c["amount_cents"],
            "txn_date": _d(c["posted_date"]),
            "payee": c["payee"] or "",
            "memo": c["memo"] or "",
        }
        # Same 21-day candidate window the live path uses.
        candidates = [
            o for o in orders
            if 0 <= (txn["txn_date"] - o["order_date"]).days <= 21
        ]
        best = find_best_match(candidates, txn, source="amazon")
        if best:
            links.append((c["id"], best["id"],
                          match_score(best, txn, source="amazon")))

    print(f"charges without a link: {len(charges)}")
    print(f"matches found:          {len(links)}")
    for txn_id, order_id, score in links[:10]:
        print(f"  ledger {txn_id} -> order {order_id}  score={score:.3f}")
    if len(links) > 10:
        print(f"  ... and {len(links) - 10} more")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to commit.")
        return

    with storage.connect(args.db) as con:
        for txn_id, order_id, score in links:
            con.execute(
                "UPDATE ledger_txn SET pending_order_id = ?, match_score = ? "
                "WHERE id = ? AND pending_order_id IS NULL",
                (order_id, score, txn_id),
            )
            con.execute(
                "UPDATE pending_order SET status = 'matched' WHERE id = ?",
                (order_id,),
            )
    storage.audit(args.db, "amazon_match_backfill", {"linked": len(links)})
    print(f"\nAPPLIED — {len(links)} links written.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Dry-run it against the live DB**

Run: `.venv/Scripts/python.exe scripts/backfill_amazon_matches.py`
Expected: `charges without a link: 103` (approximately) and `matches found: 44` (approximately). If `matches found` is 0, stop — the candidate window or the row filter is wrong, not the matcher.

- [ ] **Step 3: Apply**

Run: `.venv/Scripts/python.exe scripts/backfill_amazon_matches.py --apply`
Expected: `APPLIED — 44 links written.`

- [ ] **Step 4: Verify idempotence**

Run: `.venv/Scripts/python.exe scripts/backfill_amazon_matches.py`
Expected: `matches found: 0` — every charge it could link now carries a link, so the `pending_order_id IS NULL` filter excludes them.

- [ ] **Step 5: Commit**

```bash
git add scripts/backfill_amazon_matches.py
git commit -m "feat(scripts): backfill historical Amazon charge<->order links"
```

---

# Phase 2 — One holding category

### Task 4: Collapse the Amazon fork

**Files:**
- Modify: `bot/ingest.py` (`:190`, `:219–239`, `:335–345`, `:952–990`)
- Test: `tests/test_amazon_matching.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `_amazon_uncategorized_category(db_path) -> str | None` — resolves the `Amazon Uncategorized` category id by name. Replaces `_amazon_bucket_category`.

- [ ] **Step 1: Write the failing tests**

```python
def test_every_amazon_charge_lands_in_one_category(tmp_path):
    """No threshold: an $815 charge and a $12 charge file identically."""
    db = _setup(tmp_path)
    for cents, day in [(-81509, 12), (-1249, 13)]:
        ingest.ingest_signal(
            db,
            parsed={
                "kind": "cc_alert", "payee": "AMAZON MKTPLACE PMTS",
                "amount_cents": cents, "posted_date": date(2026, 7, day),
                "last4": "1111", "summary": "AMAZON MKTPLACE PMTS",
            },
            user_id="steven",
        )
    with storage.connect(db) as con:
        cats = [r["category_id"] for r in con.execute(
            "SELECT category_id FROM ledger_txn ORDER BY posted_date")]
        holds = con.execute(
            "SELECT COUNT(*) c FROM pending_txn WHERE queue_lane = 'hold'"
        ).fetchone()["c"]
    assert cats == ["cat-amz-unc", "cat-amz-unc"]
    assert holds == 0


def test_large_amazon_creates_no_pending_txn(tmp_path):
    """The old design queued >=$150 for a confirm prompt. It must not now."""
    db = _setup(tmp_path)
    ingest.ingest_signal(
        db,
        parsed={
            "kind": "cc_alert", "payee": "AMAZON MKTPLACE PMTS",
            "amount_cents": -81509, "posted_date": date(2026, 7, 12),
            "last4": "1111", "summary": "AMAZON MKTPLACE PMTS",
        },
        user_id="steven",
    )
    with storage.connect(db) as con:
        n = con.execute("SELECT COUNT(*) c FROM pending_txn").fetchone()["c"]
    assert n == 0
```

- [ ] **Step 2: Run and watch them fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_amazon_matching.py -v`
Expected: both FAIL — the $815 charge currently has `category_id IS NULL` and a HOLD-lane `pending_txn`.

- [ ] **Step 3: Add the resolver**

Replace `_amazon_bucket_category` (`bot/ingest.py:970`) with:

```python
def _amazon_uncategorized_category(db_path: Path | str) -> str | None:
    """Resolve the single Amazon holding category.

    Spec 2026-08-01: the three per-person buckets are retired for one
    'Amazon Uncategorized' the household drains. Person attribution
    survives in the memo, not in the category. Looked up by name so a
    re-created category still resolves.
    """
    with storage.connect(db_path) as con:
        row = con.execute(
            "SELECT id FROM category WHERE name = 'Amazon Uncategorized'"
        ).fetchone()
    return row["id"] if row else None
```

Delete `_AMAZON_BUCKET_NAMES`, `LARGE_AMAZON_DEFAULT_CENTS`, and
`_is_large_amazon_charge`.

- [ ] **Step 4: Collapse the fork**

At `:190`, delete the `is_large_amazon` assignment.

At `:231`, replace the whole `if is_amazon and not is_large_amazon:` / `elif` head with:

```python
        ledger_category_id: str | None = None
        if is_amazon:
            # Spec 2026-08-01: every Amazon charge lands here regardless of
            # size, and is drained to a real category from the Amazon panel
            # or by replying to the group ping. Person rides in the memo via
            # _enrich_from_pending_order, not in the category.
            ledger_category_id = _amazon_uncategorized_category(db_path)
            storage.audit(db_path, "amazon_uncategorized_file", {
                "payee": payee,
                "person": (matched_order or {}).get("assigned_to_user_id")
                          or "unassigned",
                "matched_order": bool(matched_order),
            })
        elif settings is not None:
```

Leave the `elif settings is not None:` body exactly as it is.

At `:344`, restore the original Amazon exclusion — Amazon never enters the prompt queue now:

```python
    if (action == "new" and signal_kind in _PROMPT_USER_KINDS
            and user_id and not is_amazon):
```

Update the comment block above it to drop the large-charge paragraph.

- [ ] **Step 5: Run and watch them pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_amazon_matching.py -v`
Expected: all pass.

- [ ] **Step 5a: Write the person into the memo**

Spec decision 3 says person attribution survives in the memo. It does **not**
today — `_enrich_from_pending_order` writes only
`"Amazon order <ext_id>: <items>"`. With the per-person categories gone, the
memo becomes the only place the fact is stored, so this must be added.

First the test:

```python
def test_person_lands_in_the_memo(tmp_path):
    db = _setup(tmp_path)
    _insert_order(db, order_id=2, total_cents=4289,
                  order_date="2026-07-10", person="allison")
    ingest.ingest_signal(
        db,
        parsed={
            "kind": "cc_alert", "payee": "AMAZON MKTPLACE PMTS",
            "amount_cents": -4289, "posted_date": date(2026, 7, 12),
            "last4": "1111", "summary": "AMAZON MKTPLACE PMTS",
        },
        user_id="steven",
    )
    with storage.connect(db) as con:
        memo = con.execute("SELECT memo FROM ledger_txn").fetchone()["memo"]
    assert "Allison" in memo
```

Run it, watch it fail, then in `_enrich_from_pending_order` append the person to
the summary it already builds:

```python
    person = (best.get("assigned_to_user_id") or "").lower()
    label = {"steven": "Steven", "allison": "Allison"}.get(person)
    if label:
        new_summary = f"{new_summary} [{label}]"
```

placed immediately before `parsed["summary"] = new_summary`.

Run it again and watch it pass.

- [ ] **Step 6: Remove the retro sweep's threshold guard**

In `retro_bucket_amazon_order` (`:1014–1021`), delete the
`if _is_large_amazon_charge(...)` early-return block and its comment. The
function must still write the link (Task 2) and now also re-file to
`Amazon Uncategorized` rather than a person bucket.

- [ ] **Step 7: Run the full suite**

Run: `.venv/Scripts/python.exe -m pytest tests/ -v`
Expected: `tests/test_large_amazon.py` now FAILS — it asserts the deleted
constants. That is correct; it is deleted in Task 5. Every other test passes.

- [ ] **Step 8: Commit**

```bash
git add bot/ingest.py tests/test_amazon_matching.py
git commit -m "feat(amazon): every charge files to one holding category"
```

---

### Task 5: Delete the dead machinery

**Files:**
- Delete: `bot/amazon_tracker.py`, `tests/test_large_amazon.py`
- Modify: `bot/queue_lane.py`, `bot/batch_processor.py`, `bot/telegram_bot.py`, `bot/config.py`, `config.yaml`

**Interfaces:**
- Consumes: Task 4.
- Produces: nothing. This task only removes.

- [ ] **Step 1: Delete the files**

```bash
git rm bot/amazon_tracker.py tests/test_large_amazon.py
```

- [ ] **Step 2: Remove every reference**

Run: `grep -rn "amazon_tracker\|build_amazon_batch\|count_amazon_ready\|AMAZON_HOLD_TTL_DAYS\|amazon_aged_out\|_is_large_amazon_charge\|large_charge_cents" bot/ scripts/ tests/ config.yaml`

Remove each hit:

- `bot/telegram_bot.py:2181` `_amazon_cmd` and `:2401` its `CommandHandler` registration.
- `bot/telegram_bot.py:1306–1309` the `header_label == "Amazon"` branch.
- `bot/telegram_bot.py:1360–1364`, `:1467–1475` the `amazon_ready` footer counters — and the `amazon_ready=` keyword on the formatter they call.
- `bot/telegram_bot.py:1955–1958` the `send_aged_out_alert_if_new` call.
- `bot/telegram_bot.py:2142` the `/amazon` line in the help text.
- `bot/batch_processor.py:78–100` `build_amazon_batch`, `:117+` `count_amazon_ready`.
- `bot/queue_lane.py` — `AMAZON_HOLD_TTL_DAYS` and the `amazon_aged_out` branch of `abandon_stale_holds()`.
- `bot/config.py` — `AmazonConfig.large_charge_cents`, and `AmazonConfig` itself if that was its only field.
- `config.yaml` — the `amazon:` block if now empty.

- [ ] **Step 3: Prove nothing references the deleted names**

Run: `grep -rn "amazon_tracker\|build_amazon_batch\|count_amazon_ready\|AMAZON_HOLD_TTL_DAYS\|amazon_aged_out\|_is_large_amazon_charge\|large_charge_cents" bot/ scripts/ tests/ config.yaml`
Expected: no output.

- [ ] **Step 4: Prove the bot still imports**

Run: `.venv/Scripts/python.exe -c "from bot import telegram_bot, queue_lane, batch_processor, ingest; print('ok')"`
Expected: `ok`. An `ImportError` here means a reference was missed.

- [ ] **Step 5: Run the full suite**

Run: `.venv/Scripts/python.exe -m pytest tests/ -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "chore(amazon): delete the buckets, HOLD lane, /amazon and the large-charge path"
```

---

### Task 6: Retire the buckets in the live DB

**Files:**
- Create: `scripts/retire_amazon_buckets.py`

**Interfaces:**
- Consumes: `bot.envelope.move_money`, `bot.storage`.
- Produces: a CLI. `--dry-run` default; `--apply` commits.

- [ ] **Step 1: Write the script**

```python
"""Retire the three per-person Amazon buckets for one holding category.

Spec: docs/superpowers/specs/2026-08-01-amazon-parsing-panel-design.md

Creates 'Amazon Uncategorized' under Day to Day Expenses, moves the
buckets' available balances into it, hides the buckets, and re-files any
still-open Amazon pending_txn rows.

The moves are ORDERED because /envelope/move and move_money reject
non-positive amounts and two buckets carry NEGATIVE available:
  1. Amazon - Steven  -> Amazon Uncategorized  (its positive balance)
  2. Amazon Uncategorized -> Amazon - Allison     (cover its overspend)
  3. Amazon Uncategorized -> Amazon - Unassigned  (cover its overspend)

Balances are READ LIVE, never hardcoded: as of 2026-08-01 the carried
values did not chain cleanly (Allison ends July at -$39.64 but carries
-$305.78 into August), so the arithmetic must come from the DB at
execution time and be eyeballed before --apply.

Historical ledger rows are NOT repointed and month_category is NOT
recomputed — standing rule, measured at +$10.5k phantom RTA.

Usage:
    .venv/Scripts/python.exe scripts/retire_amazon_buckets.py
    .venv/Scripts/python.exe scripts/retire_amazon_buckets.py --apply
"""
from __future__ import annotations

import argparse
import uuid
from pathlib import Path

from bot import envelope, storage

BUCKETS = ["Amazon - Steven", "Amazon - Allison", "Amazon - Unassigned"]
NEW_NAME = "Amazon Uncategorized"
GROUP = "Day to Day Expenses"


def _current_month(con) -> str:
    row = con.execute("SELECT MAX(month) m FROM month_category").fetchone()
    return row["m"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db", default="ynab_helper.db")
    args = ap.parse_args()

    with storage.connect(args.db) as con:
        month = _current_month(con)
        group = con.execute(
            "SELECT id FROM category_group WHERE name = ?", (GROUP,)
        ).fetchone()
        if not group:
            raise SystemExit(f"category group {GROUP!r} not found")
        new_cat = con.execute(
            "SELECT id FROM category WHERE name = ?", (NEW_NAME,)
        ).fetchone()
        rows = []
        for name in BUCKETS:
            c = con.execute(
                "SELECT id FROM category WHERE name = ?", (name,)
            ).fetchone()
            if not c:
                continue
            mc = con.execute(
                "SELECT available_cents FROM month_category "
                "WHERE category_id = ? AND month = ?",
                (c["id"], month),
            ).fetchone()
            rows.append((name, c["id"], (mc["available_cents"] if mc else 0)))

    print(f"month: {month}")
    print(f"{'bucket':<24}{'available':>14}")
    total = 0
    for name, _cid, avail in rows:
        total += avail
        print(f"{name:<24}{avail/100:>13,.2f}")
    print(f"{'NET -> ' + NEW_NAME:<24}{total/100:>13,.2f}")
    print()
    print("Planned moves:")
    for name, _cid, avail in rows:
        if avail > 0:
            print(f"  {name} -> {NEW_NAME}: {avail/100:,.2f}")
        elif avail < 0:
            print(f"  {NEW_NAME} -> {name}: {abs(avail)/100:,.2f} (cover overspend)")
        else:
            print(f"  {name}: already 0, no move")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to commit.")
        return

    new_id = new_cat["id"] if new_cat else str(uuid.uuid4())
    with storage.connect(args.db) as con:
        if not new_cat:
            con.execute(
                "INSERT INTO category (id, group_id, name, hidden, is_spending) "
                "VALUES (?, ?, ?, 0, 0)",
                (new_id, group["id"], NEW_NAME),
            )
            print(f"created {NEW_NAME} ({new_id})")

    # Positive balances IN first, so the category can fund the covers.
    for name, cid, avail in sorted(rows, key=lambda r: -r[2]):
        if avail > 0:
            envelope.move_money(args.db, month=month, from_category_id=cid,
                                to_category_id=new_id, cents=avail)
        elif avail < 0:
            envelope.move_money(args.db, month=month, from_category_id=new_id,
                                to_category_id=cid, cents=abs(avail))

    with storage.connect(args.db) as con:
        for name, cid, _a in rows:
            con.execute("UPDATE category SET hidden = 1 WHERE id = ?", (cid,))
        # Close any Amazon rows still sitting in the confirm queue — the
        # queue no longer receives Amazon at all.
        con.execute(
            "UPDATE pending_txn SET status = 'closed' "
            "WHERE status = 'pending' "
            "  AND (UPPER(payee) LIKE '%AMAZON%' OR UPPER(payee) LIKE '%AMZN%')"
        )
        # Any ledger row still pointing at a bucket in the CURRENT month
        # moves to the new category; history stays put.
        con.execute(
            "UPDATE ledger_txn SET category_id = ? "
            "WHERE category_id IN (SELECT id FROM category WHERE name IN "
            "  ('Amazon - Steven','Amazon - Allison','Amazon - Unassigned')) "
            "  AND posted_date >= ?",
            (new_id, f"{month}-01"),
        )
    storage.audit(args.db, "amazon_buckets_retired",
                  {"month": month, "new_category_id": new_id})
    print("\nAPPLIED.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Dry-run and READ THE OUTPUT**

Run: `.venv/Scripts/python.exe scripts/retire_amazon_buckets.py`

Expected shape (numbers will differ — they are read live):

```
Amazon - Steven              1,288.18
Amazon - Allison              -305.78
Amazon - Unassigned           -192.50
NET -> Amazon Uncategorized     789.90
```

**Stop and show this to Steven before applying.** If the net is negative, the
covers cannot be funded from the moves alone and the script will fail partway —
report that instead of running `--apply`.

- [ ] **Step 3: Apply, after approval**

Run: `.venv/Scripts/python.exe scripts/retire_amazon_buckets.py --apply`

- [ ] **Step 4: Verify**

```bash
.venv/Scripts/python.exe -c "
import sqlite3;con=sqlite3.connect('ynab_helper.db');con.row_factory=sqlite3.Row
for r in con.execute(\"SELECT c.name,c.hidden,mc.available_cents/100.0 av FROM category c LEFT JOIN month_category mc ON mc.category_id=c.id AND mc.month=(SELECT MAX(month) FROM month_category) WHERE c.name LIKE 'Amazon%'\"): print(dict(r))"
```

Expected: the three buckets `hidden=1` with `av` 0.0, and `Amazon Uncategorized` `hidden=0` holding the net.

- [ ] **Step 5: Commit**

```bash
git add scripts/retire_amazon_buckets.py
git commit -m "feat(scripts): retire the per-person Amazon buckets"
```

---

# Phase 3 — The panel

### Task 7: Make the reconcile query read the persisted link

**Files:**
- Modify: `src-tauri/src/commands.rs` (`q_amazon_reconcile`, `:1556`+)
- Modify: `bot/webui_queries.py` (add `q_amazon_reconcile`)
- Modify: `src/lib/types.ts`, `src/lib/mockData.ts`

**Interfaces:**
- Consumes: `ledger_txn.pending_order_id` from Phase 1.
- Produces: `AmazonReconcile` gains `months: AmazonMonthStat[]` where
  `AmazonMonthStat = { month: string; charges: number; matched: number }`.
  `AmazonCharge` gains `who: string | null` and `pending_order_id: number | null`.
  The existing fields and the `counts` block keep their names and meanings.

- [ ] **Step 1: Delete the Rust pairing logic**

In `q_amazon_reconcile`, the block that begins at the `SHIP_WINDOW_DAYS` /
`EMAIL_LAG_DAYS` constants and builds `pairs` — through to where `matched`,
`unmatched_charges`, and `unmatched_orders` are assembled — is replaced. The two
loading queries above it stay.

Reason, worth a comment in the code: this logic used different rules than
`bot/matcher.py`, so the panel displayed matches the bot never made. The bot's
matcher is now the only one, and its result is persisted.

Add `t.pending_order_id` to the charges SELECT, plus the person via the linked
order:

```sql
SELECT t.id, t.posted_date, t.amount_cents, t.payee,
       a.name AS account_name, t.memo, c.name AS category_name,
       t.pending_order_id, po.assigned_to_user_id AS who
FROM ledger_txn t
JOIN account a ON a.id = t.account_id
LEFT JOIN category c ON c.id = t.category_id
LEFT JOIN pending_order po ON po.id = t.pending_order_id
WHERE (UPPER(t.payee) LIKE '%AMAZON%' OR UPPER(t.payee) LIKE '%AMZN%')
  AND t.parent_txn_id IS NULL
  AND t.amount_cents < 0
  AND (t.payee IS NULL OR t.payee NOT LIKE 'Transfer :%')
  AND t.transfer_account_id IS NULL
  AND a.on_budget = 1
  AND t.posted_date >= ?1
ORDER BY t.posted_date DESC, t.id DESC
```

Then partition in Rust with no scoring at all:

- `matched` — charges with `pending_order_id IS NOT NULL`, joined to their order.
- `unmatched_charges` — `pending_order_id IS NULL` **and** at least 4 days old.
- `unmatched_orders` — orders with no charge pointing at them, at least 4 days old.

The 4-day floor keeps charges that simply have not had time to match out of the
"unmatched" bucket, matching the Telegram rule.

- [ ] **Step 2: Add the per-month stats**

```sql
SELECT substr(t.posted_date,1,7) AS month,
       COUNT(*) AS charges,
       SUM(CASE WHEN t.pending_order_id IS NOT NULL THEN 1 ELSE 0 END) AS matched
FROM ledger_txn t
JOIN account a ON a.id = t.account_id
WHERE (UPPER(t.payee) LIKE '%AMAZON%' OR UPPER(t.payee) LIKE '%AMZN%')
  AND t.parent_txn_id IS NULL AND t.amount_cents < 0
  AND (t.payee IS NULL OR t.payee NOT LIKE 'Transfer :%')
  AND t.transfer_account_id IS NULL AND a.on_budget = 1
  AND t.posted_date >= ?1
GROUP BY 1 ORDER BY 1 DESC LIMIT 6
```

This is what keeps July's 94% from being averaged into a misleading lifetime rate.

- [ ] **Step 3: Mirror it in Python for HTTP mode**

Add `q_amazon_reconcile(db_path: str, days: int = 180, **_) -> dict` to
`bot/webui_queries.py`, returning the identical JSON shape using the same SQL.
Follow the existing `q_*` conventions in that file.

- [ ] **Step 4: Update the TypeScript types and the mock**

In `src/lib/types.ts` add `who` and `pending_order_id` to `AmazonCharge`, and
`months: AmazonMonthStat[]` plus the new interface to `AmazonReconcile`.

In `src/lib/mockData.ts`, extend the Amazon fixture to the new shape so
`npm run dev` still renders.

- [ ] **Step 5: Build both sides**

Run (bot repo): `.venv/Scripts/python.exe -m pytest tests/ -v` → all pass.
Run (UI repo): `npm run build` → PASS.
Run (UI repo): `npm run tauri build` → PASS. This compiles the Rust; a
TypeScript-only build will not catch a rusqlite column-index error.

- [ ] **Step 6: Commit**

```bash
git add src-tauri/src/commands.rs src/lib/types.ts src/lib/mockData.ts
git commit -m "refactor(amazon): read the persisted match instead of re-matching in Rust"
```

and in the bot repo:

```bash
git add bot/webui_queries.py
git commit -m "feat(webui): q_amazon_reconcile for HTTP mode"
```

---

### Task 8: Manual match and unmatch endpoints

**Files:**
- Modify: `bot/http_api.py`
- Modify: `src/lib/api.ts`
- Test: `tests/test_amazon_matching.py`

**Interfaces:**
- Consumes: Phase 1 columns.
- Produces:
  - `POST /amazon/match {ledger_txn_id: int, pending_order_id: int} -> {ok: true}`
  - `POST /amazon/unmatch {ledger_txn_id: int} -> {ok: true}`
  - TS: `amazonMatch({ ledger_txn_id, pending_order_id })`, `amazonUnmatch({ ledger_txn_id })`.

- [ ] **Step 1: Write the failing tests**

```python
def test_manual_match_links_and_marks(tmp_path):
    from bot import http_api
    db = _setup(tmp_path)
    _insert_order(db, order_id=3, total_cents=3429, order_date="2026-06-06")
    with storage.connect(db) as con:
        con.execute(
            "INSERT INTO ledger_txn (id, account_id, posted_date, "
            "  amount_cents, payee, is_split) "
            "VALUES (900, ?, '2026-06-08', -3429, 'Amazon', 0)", (ACCT,))
    http_api._amazon_link(db, ledger_txn_id=900, pending_order_id=3)
    with storage.connect(db) as con:
        assert con.execute(
            "SELECT pending_order_id FROM ledger_txn WHERE id = 900"
        ).fetchone()["pending_order_id"] == 3
        assert con.execute(
            "SELECT status FROM pending_order WHERE id = 3"
        ).fetchone()["status"] == "matched"


def test_unmatch_reverts_order_only_when_last(tmp_path):
    from bot import http_api
    db = _setup(tmp_path)
    _insert_order(db, order_id=4, total_cents=2000, order_date="2026-06-06")
    with storage.connect(db) as con:
        for tid in (901, 902):
            con.execute(
                "INSERT INTO ledger_txn (id, account_id, posted_date, "
                "  amount_cents, payee, is_split, pending_order_id) "
                "VALUES (?, ?, '2026-06-08', -1000, 'Amazon', 0, 4)",
                (tid, ACCT))
        con.execute("UPDATE pending_order SET status='matched' WHERE id=4")
    http_api._amazon_unlink(db, ledger_txn_id=901)
    with storage.connect(db) as con:
        # 902 still points at it, so the order stays matched
        assert con.execute(
            "SELECT status FROM pending_order WHERE id = 4"
        ).fetchone()["status"] == "matched"
    http_api._amazon_unlink(db, ledger_txn_id=902)
    with storage.connect(db) as con:
        assert con.execute(
            "SELECT status FROM pending_order WHERE id = 4"
        ).fetchone()["status"] == "pending"
```

- [ ] **Step 2: Run and watch them fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_amazon_matching.py -v`
Expected: FAIL — `module 'bot.http_api' has no attribute '_amazon_link'`.

- [ ] **Step 3: Implement the helpers at module scope**

In `bot/http_api.py`, **at module scope** (not inside `build_app`):

```python
class AmazonMatchBody(BaseModel):
    ledger_txn_id: int
    pending_order_id: int


class AmazonUnmatchBody(BaseModel):
    ledger_txn_id: int


def _amazon_link(db_path, *, ledger_txn_id: int, pending_order_id: int) -> None:
    """Manually link a charge to an order.

    Exists because the matcher's ambiguity guard correctly refuses to
    guess between near-identical orders (June's $34.29 / $34.31 twins
    both scored ~1.0 and were both rejected). A human breaking the tie is
    the intended resolution — there was previously nowhere to do it.
    """
    with storage.connect(db_path) as con:
        con.execute(
            "UPDATE ledger_txn SET pending_order_id = ? WHERE id = ?",
            (pending_order_id, ledger_txn_id),
        )
        con.execute(
            "UPDATE pending_order SET status = 'matched', "
            "  updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (pending_order_id,),
        )
    storage.audit(db_path, "amazon_manual_match", {
        "ledger_txn_id": ledger_txn_id, "pending_order_id": pending_order_id,
    })


def _amazon_unlink(db_path, *, ledger_txn_id: int) -> None:
    """Clear a bad link; revert the order only when nothing else points at it."""
    with storage.connect(db_path) as con:
        row = con.execute(
            "SELECT pending_order_id FROM ledger_txn WHERE id = ?",
            (ledger_txn_id,),
        ).fetchone()
        order_id = row["pending_order_id"] if row else None
        con.execute(
            "UPDATE ledger_txn SET pending_order_id = NULL, match_score = NULL "
            "WHERE id = ?",
            (ledger_txn_id,),
        )
        if order_id is not None:
            remaining = con.execute(
                "SELECT COUNT(*) c FROM ledger_txn WHERE pending_order_id = ?",
                (order_id,),
            ).fetchone()["c"]
            if remaining == 0:
                con.execute(
                    "UPDATE pending_order SET status = 'pending' WHERE id = ?",
                    (order_id,),
                )
    storage.audit(db_path, "amazon_manual_unmatch", {
        "ledger_txn_id": ledger_txn_id, "pending_order_id": order_id,
    })
```

- [ ] **Step 4: Run and watch them pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_amazon_matching.py -v`
Expected: all pass.

- [ ] **Step 5: Wire the routes**

Inside `build_app()`, **above** the catch-all `GET /{full_path:path}`:

```python
    @app.post("/amazon/match", dependencies=[Depends(_require_token)])
    def amazon_match(body: AmazonMatchBody) -> dict[str, Any]:
        _amazon_link(db_path, ledger_txn_id=body.ledger_txn_id,
                     pending_order_id=body.pending_order_id)
        return {"ok": True}

    @app.post("/amazon/unmatch", dependencies=[Depends(_require_token)])
    def amazon_unmatch(body: AmazonUnmatchBody) -> dict[str, Any]:
        _amazon_unlink(db_path, ledger_txn_id=body.ledger_txn_id)
        return {"ok": True}
```

- [ ] **Step 6: Add the TS client**

In `src/lib/api.ts`:

```ts
export function amazonMatch(opts: {
  ledger_txn_id: number;
  pending_order_id: number;
}): Promise<{ ok: boolean }> {
  return apiPost("/amazon/match", opts);
}

export function amazonUnmatch(opts: {
  ledger_txn_id: number;
}): Promise<{ ok: boolean }> {
  return apiPost("/amazon/unmatch", opts);
}
```

- [ ] **Step 7: Commit**

```bash
git add bot/http_api.py tests/test_amazon_matching.py
git commit -m "feat(api): manual Amazon match and unmatch"
```

---

### Task 9: Promote the panel

**Files:**
- Create: `src/pages/Amazon.tsx`
- Modify: `src/pages/Reconciler.tsx`, `src/pages/Transactions.tsx`, `src/App.tsx`

**Interfaces:**
- Consumes: `getAmazonReconcile` (`@/lib/db`), `amazonMatch`/`amazonUnmatch`/`categorizeTransaction` (`@/lib/api`), `CategoryPicker`, `Modal`.
- Produces: default export `AmazonPage` at route `/amazon`, accepting `?charge=<ledger_txn_id>` for deep links.

- [ ] **Step 1: Move the view**

Create `src/pages/Amazon.tsx` from Reconciler's `AmazonView` (`:944`) plus its
row components `AmazonChargeRow` (`:1109`), `AmazonOrderRow` (`:1148`), and
`AmazonMatchedRow` (`:1176`). Wrap it in `PageShell` as the other pages do, and
read `?charge=` from the URL for the existing flash-highlight behaviour.

- [ ] **Step 2: Delete it from Reconciler**

Remove from `src/pages/Reconciler.tsx`: the `"amazon"` member of the `View`
union (`:49`), its `VIEWS` entry (`:53`), the `{view === "amazon" && ...}` render
(`:111`), the four Amazon components, the `getAmazonReconcile` import (`:26`),
the Amazon type imports (`:36–38`), and the `viewParam === "amazon"` branch
(`:65`).

- [ ] **Step 3: Repoint the deep link**

In `src/pages/Transactions.tsx`, change the link that targets
`/reconciler?view=amazon&charge=<id>` to `/amazon?charge=<id>`.

- [ ] **Step 4: Register the route and nav entry**

In `src/App.tsx`:

```tsx
import AmazonPage from "@/pages/Amazon";
```

```tsx
<Route path="/amazon" element={<AmazonPage />} />
```

and in `NAV_GROUPS`, into the **Money** group after "Personal expenses":

```tsx
{ to: "/amazon", icon: ShoppingBag, label: "Amazon" },
```

`ShoppingBag` is already the icon Reconciler used for the tab; add it to the
`lucide-react` import if not already there.

- [ ] **Step 5: Add the header**

Above the three lists, a row of stat tiles: `Amazon Uncategorized` available,
rows still in it, dollars still to drain, and the last-6-months
charges/matched from `months`. Follow the existing `HeaderCards`/`StatTile`
patterns rather than inventing a new one.

- [ ] **Step 6: Add the Who column**

Render `charge.who` (`"steven"` / `"allison"` / null) as `Steven` / `Allison` /
`—` on the charge and matched rows, with a filter control that narrows all three
lists, and a per-person total.

- [ ] **Step 7: Add the drain action**

On charge and matched rows, a button opening a `Modal` containing
`CategoryPicker` with `excludeId` set to the Amazon Uncategorized id. On pick,
call `categorizeTransaction({ ledger_txn_id, category_id })` and invalidate:

```ts
qc.invalidateQueries({ queryKey: ["amazon_reconcile"] });
qc.invalidateQueries({ queryKey: ["month_categories"] });
qc.invalidateQueries({ queryKey: ["transactions"] });
qc.invalidateQueries({ queryKey: ["ready_to_assign"] });
```

- [ ] **Step 8: Add manual match**

On an unmatched charge row, a "Match…" button listing the unmatched orders
within ±21 days of the charge date, closest amount first. Picking one calls
`amazonMatch` and invalidates `["amazon_reconcile"]`. On a matched row, an
"Unmatch" affordance calling `amazonUnmatch`.

Do not display `match_score` anywhere.

- [ ] **Step 9: Build**

Run: `npm run build` → PASS.
Run: `npm run tauri build` → PASS.

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "feat(amazon): promote the Amazon view to a working panel"
```

---

# Phase 4 — Telegram

### Task 10: The matched ping

**Files:**
- Create: `bot/amazon_notify.py`
- Modify: `bot/ingest.py`, `bot/telegram_bot.py`
- Test: `tests/test_amazon_matching.py`

**Interfaces:**
- Consumes: Phase 1 columns, `bot.group_chat.report_target`.
- Produces:
  - `should_ping_matched(db_path, ledger_txn_id) -> bool` — dedup predicate.
  - `format_matched_ping(db_path, ledger_txn_id) -> str | None`
  - `async send_matched_pings(app) -> int` — sends for every newly-linked charge, returns the count.

- [ ] **Step 1: Write the failing dedup test**

```python
def test_matched_ping_fires_once(tmp_path):
    from bot import amazon_notify
    db = _setup(tmp_path)
    _insert_order(db, order_id=5, total_cents=4732, order_date="2026-07-28")
    with storage.connect(db) as con:
        con.execute(
            "INSERT INTO ledger_txn (id, account_id, posted_date, "
            "  amount_cents, payee, is_split, pending_order_id) "
            "VALUES (910, ?, '2026-07-29', -4732, 'Amazon', 0, 5)", (ACCT,))
    assert amazon_notify.should_ping_matched(db, 910) is True
    storage.audit(db, "amazon_matched_pinged", {"ledger_txn_id": 910})
    assert amazon_notify.should_ping_matched(db, 910) is False


def test_matched_ping_text_carries_items_and_person(tmp_path):
    from bot import amazon_notify
    db = _setup(tmp_path)
    _insert_order(db, order_id=6, total_cents=4732,
                  order_date="2026-07-28", person="allison")
    with storage.connect(db) as con:
        con.execute(
            "INSERT INTO ledger_txn (id, account_id, posted_date, "
            "  amount_cents, payee, is_split, pending_order_id) "
            "VALUES (911, ?, '2026-07-29', -4732, 'Amazon', 0, 6)", (ACCT,))
    text = amazon_notify.format_matched_ping(db, 911)
    assert "$47.32" in text
    assert "Dish Soap" in text
    assert "Allison" in text
```

- [ ] **Step 2: Run and watch them fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_amazon_matching.py -v`
Expected: FAIL — no module `bot.amazon_notify`.

- [ ] **Step 3: Write the module**

```python
"""Amazon group-chat pings: matched receipts, and 4-day unmatched charges.

Spec: docs/superpowers/specs/2026-08-01-amazon-parsing-panel-design.md

Both pings are fire-and-forget. Replying files the charge out of Amazon
Uncategorized; ignoring costs nothing, because the charge is already
filed there either way. Dedup is per charge per type, via audit events.

Volume note: the matcher runs at 94% (July, the receipt-capture ceiling),
so the unmatched ping is expected to fire roughly once a month. It is a
safety net, not a workflow.
"""
from __future__ import annotations

import logging
from pathlib import Path

from bot import storage

log = logging.getLogger(__name__)

_PERSON_LABEL = {"steven": "Steven", "allison": "Allison"}


def _fmt(cents: int) -> str:
    return f"${abs(cents) / 100:,.2f}"


def _already_pinged(db_path: Path | str, event: str, ledger_txn_id: int) -> bool:
    with storage.connect(db_path) as con:
        row = con.execute(
            "SELECT 1 FROM audit_log WHERE event = ? AND details LIKE ?",
            (event, f'%"ledger_txn_id": {ledger_txn_id}%'),
        ).fetchone()
    return row is not None


def should_ping_matched(db_path: Path | str, ledger_txn_id: int) -> bool:
    return not _already_pinged(db_path, "amazon_matched_pinged", ledger_txn_id)


def should_ping_unmatched(db_path: Path | str, ledger_txn_id: int) -> bool:
    return not _already_pinged(db_path, "amazon_unmatched_pinged", ledger_txn_id)


def format_matched_ping(db_path: Path | str, ledger_txn_id: int) -> str | None:
    with storage.connect(db_path) as con:
        row = con.execute(
            "SELECT t.amount_cents, t.posted_date, po.raw_summary, "
            "       po.assigned_to_user_id AS who "
            "FROM ledger_txn t "
            "JOIN pending_order po ON po.id = t.pending_order_id "
            "WHERE t.id = ?",
            (ledger_txn_id,),
        ).fetchone()
    if not row:
        return None
    date_str = str(row["posted_date"])[5:10].lstrip("0").replace("-0", "/").replace("-", "/")
    summary = (row["raw_summary"] or "").split(": ", 1)[-1].strip()
    who = _PERSON_LABEL.get((row["who"] or "").lower())
    parts = [f"\U0001F170 {_fmt(row['amount_cents'])} Amazon ({date_str})"]
    if summary:
        parts.append(f" \u2014 {summary}")
    if who:
        parts.append(f" \u00b7 {who}")
    return "".join(parts) + "\nReply with a category to file it."


def format_unmatched_ping(db_path: Path | str, ledger_txn_id: int) -> str | None:
    with storage.connect(db_path) as con:
        row = con.execute(
            "SELECT amount_cents, posted_date FROM ledger_txn WHERE id = ?",
            (ledger_txn_id,),
        ).fetchone()
    if not row:
        return None
    date_str = str(row["posted_date"])[5:10].lstrip("0").replace("-0", "/").replace("-", "/")
    return (
        f"\u2753 {_fmt(row['amount_cents'])} Amazon ({date_str}) "
        f"\u2014 no receipt after 4 days.\n"
        "Remember what this was? Reply with a category, or open the Amazon panel."
    )
```

- [ ] **Step 4: Run and watch them pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_amazon_matching.py -v`
Expected: all pass. If the date formatting assertion is brittle, assert on the
amount and items only and drop the date from the test — do not contort the
formatter to satisfy a test.

- [ ] **Step 5: Add the sender**

Append to `bot/amazon_notify.py`:

```python
async def _send_to_group(app, text: str) -> bool:
    """Post to the household group. Returns True if it went out."""
    from bot.group_chat import report_target
    target = report_target(app)
    if target is None:
        log.info("no group configured — skipping Amazon ping")
        return False
    gbot, group_id = target
    try:
        await gbot.send_message(chat_id=group_id, text=text)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("Amazon ping send failed: %s", e)
        return False


async def send_matched_pings(app) -> int:
    """One ping per newly-linked Amazon charge. Fire-and-forget."""
    db_path = app.bot_data["settings"].paths.database
    with storage.connect(db_path) as con:
        rows = con.execute(
            "SELECT t.id FROM ledger_txn t "
            "WHERE t.pending_order_id IS NOT NULL "
            "  AND t.posted_date >= date('now', '-30 day') "
            "ORDER BY t.posted_date"
        ).fetchall()
    sent = 0
    for r in rows:
        txn_id = r["id"]
        if not should_ping_matched(db_path, txn_id):
            continue
        text = format_matched_ping(db_path, txn_id)
        if not text:
            continue
        if await _send_to_group(app, text):
            sent += 1
        # Audit either way — a send failure must not queue a retry storm.
        storage.audit(db_path, "amazon_matched_pinged",
                      {"ledger_txn_id": txn_id})
    return sent
```

The `-30 day` bound matters: the Task 3 backfill links years of historical
charges, none of which have a ping record, so an unbounded first run would post
dozens of messages the moment the bot restarts.

- [ ] **Step 5a: Hook it in**

Call `send_matched_pings` from the same periodic loop in `bot/telegram_bot.py`
that previously called `send_aged_out_alert_if_new` (`:1955`), with the same
`try/except` shape so a ping failure cannot take the loop down.

- [ ] **Step 6: Commit**

```bash
git add bot/amazon_notify.py bot/telegram_bot.py tests/test_amazon_matching.py
git commit -m "feat(amazon): group ping when a receipt matches a charge"
```

---

### Task 11: The 4-day unmatched sweep

**Files:**
- Modify: `bot/amazon_notify.py`, `bot/telegram_bot.py`
- Test: `tests/test_amazon_matching.py`

**Interfaces:**
- Consumes: Task 10's helpers.
- Produces: `unmatched_charges(db_path, *, days: int = 4) -> list[int]`, `async send_unmatched_pings(app) -> int`.

- [ ] **Step 1: Write the failing test**

```python
def test_unmatched_sweep_respects_the_four_day_floor(tmp_path):
    from bot import amazon_notify
    db = _setup(tmp_path)
    with storage.connect(db) as con:
        con.execute(
            "INSERT INTO ledger_txn (id, account_id, posted_date, "
            "  amount_cents, payee, is_split) VALUES "
            "(920, ?, date('now','-3 day'), -2358, 'Amazon', 0)", (ACCT,))
        con.execute(
            "INSERT INTO ledger_txn (id, account_id, posted_date, "
            "  amount_cents, payee, is_split) VALUES "
            "(921, ?, date('now','-5 day'), -2358, 'Amazon', 0)", (ACCT,))
    ids = amazon_notify.unmatched_charges(db, days=4)
    assert 920 not in ids   # 3 days old — still has time to match
    assert 921 in ids       # 5 days old — ask
```

- [ ] **Step 2: Run and watch it fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_amazon_matching.py::test_unmatched_sweep_respects_the_four_day_floor -v`
Expected: FAIL — no attribute `unmatched_charges`.

- [ ] **Step 3: Implement**

```python
def unmatched_charges(db_path: Path | str, *, days: int = 4) -> list[int]:
    """Amazon charges with no linked receipt after `days`.

    The window is an ASKING deadline, not a matching deadline: the
    matcher's candidate window is 21 days, so a late order email can
    still link a charge that was already pinged here. The two pings are
    deduped separately, so that charge still gets its matched ping.
    """
    with storage.connect(db_path) as con:
        rows = con.execute(
            "SELECT t.id FROM ledger_txn t "
            "JOIN account a ON a.id = t.account_id "
            "WHERE (UPPER(t.payee) LIKE '%AMAZON%' "
            "       OR UPPER(t.payee) LIKE '%AMZN%') "
            "  AND t.payee NOT LIKE 'Transfer :%' "
            "  AND t.transfer_account_id IS NULL "
            "  AND t.parent_txn_id IS NULL "
            "  AND t.amount_cents < 0 "
            "  AND a.on_budget = 1 "
            "  AND t.pending_order_id IS NULL "
            "  AND t.posted_date <= date('now', ?) "
            # Floor at 30 days: the Task 3 backfill links years of history
            # and leaves the rest unmatched with no ping record, so an
            # unbounded sweep would post dozens of messages on first run.
            "  AND t.posted_date >= date('now', '-30 day') "
            "ORDER BY t.posted_date",
            (f"-{int(days)} day",),
        ).fetchall()
    return [r["id"] for r in rows
            if should_ping_unmatched(db_path, r["id"])]
```

- [ ] **Step 4: Run and watch it pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_amazon_matching.py -v`
Expected: all pass.

- [ ] **Step 5: Add the sender and hook it into the daily loop**

`async def send_unmatched_pings(app) -> int` mirrors `send_matched_pings`, using
`format_unmatched_ping`, `unmatched_charges`, `_send_to_group`, and writing
`amazon_unmatched_pinged`. Call it from the **daily** summary loop, not the
30-minute tick — a 4-day threshold does not need finer resolution.

The first-run guard lives in `unmatched_charges` itself (the 30-day floor added
in Step 3), so the sender needs no bound of its own.

- [ ] **Step 6: Verify the guard against the live DB**

```bash
.venv/Scripts/python.exe -c "
from bot import amazon_notify
ids = amazon_notify.unmatched_charges('ynab_helper.db', days=4)
print(len(ids), 'charges would ping')"
```

Expected: a single-digit number. If it prints dozens, the 30-day bound is not
applied — fix it before the bot restarts.

- [ ] **Step 7: Commit**

```bash
git add bot/amazon_notify.py bot/telegram_bot.py tests/test_amazon_matching.py
git commit -m "feat(amazon): ping the group for charges unmatched after 4 days"
```

---

### Task 12: End-to-end verification

**Files:** none.

- [ ] **Step 1: Full test suite**

Run: `.venv/Scripts/python.exe -m pytest tests/ -v`
Expected: all pass, no skips.

- [ ] **Step 2: Import check**

Run: `.venv/Scripts/python.exe -c "from bot import telegram_bot, ingest, http_api, amazon_notify, webui_queries; print('ok')"`

- [ ] **Step 3: Restart the bot**

Via the **Bot Control** panel or the `YNAB-Helper-Bot` scheduled task. Never PID-kill or start it from this session.

- [ ] **Step 4: Confirm the API is up**

Run: `curl -s http://127.0.0.1:8765/healthz`
Expected: a healthy response. A silent failure here usually means `post_init` did not fire, which kills every background loop and the API together.

- [ ] **Step 5: Check the panel against reality**

Open the desktop app → **Amazon**. Verify:

1. The month bar shows July at ~94% and the earlier months far lower — if every month reads the same, the per-month query is wrong.
2. Matched rows show item text and a Who value.
3. Draining a charge to a real category removes it from Amazon Uncategorized, and the header's "left to drain" drops.
4. An unmatched charge offers "Match…", and matching it moves the row into Matched.
5. Unmatching returns it.

- [ ] **Step 6: Report**

State what passed and what did not, with observed values for the July match rate and the Amazon Uncategorized balance. Do not report success on a clean test suite alone.

---

## Deployment

Ordered — the cutover crosses both processes.

1. Task 1 migration + Task 3 backfill. Safe any time; read-mostly.
2. Task 6 retirement script, **dry-run reviewed by Steven first**.
3. Bot restart (Task 12 step 3) — bot code goes live only here.
4. `npm run tauri build` + hot-swap the exe over `AppData\Local`. **Required** — Task 7 changes Rust.
5. Mobile web: `deploy_webui.ps1`. No restart.

**Keep steps 2 and 3 close together.** Between them the old ingest code is still
running and resolves `_amazon_bucket_category` to now-hidden categories, so any
Amazon charge landing in that gap files into a hidden bucket and must be
re-filed by hand.
