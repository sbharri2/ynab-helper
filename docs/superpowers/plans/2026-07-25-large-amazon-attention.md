# Large Amazon Purchases Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Amazon charges at or above $150 stop auto-filing into the per-person buckets — they hold uncategorized, wait up to 24h for the order email so the question carries item detail, then ask.

**Architecture:** The HOLD lane already implements "wait for the receipt, then ask" and `sweep_lanes()` still runs it every 30 minutes; nothing has fed it since 2026-07-03. This revives it gated on dollar amount. One fork in `bot/ingest.py`, a TTL split in `bot/queue_lane.py`, a guard on the retro-bucket path, and a one-shot repair script.

**Tech Stack:** Python 3.12, SQLite (`bot/storage.py` helpers), pydantic settings (`bot/config.py`), pytest.

**Spec:** `docs/superpowers/specs/2026-07-25-large-amazon-attention-design.md`

## Global Constraints

- Threshold default is **15000 cents ($150)**, configurable at `amazon.large_charge_cents` in `config.yaml`.
- The threshold test is **signed**: `amount_cents <= -threshold`. Never `abs()`. Refunds must not trigger questions.
- **Nothing auto-commits at or above the threshold** — not a confirmed prior, not a matched order's `chosen_category`, not a payee override.
- **No schema migration.** Large-Amazon holds are identified by payee + amount, never a new column.
- Never PID-kill or start the bot. Code goes live when the `YNAB-Helper-Bot` scheduled task restarts.
- Run Python via `.venv/Scripts/python.exe` (PowerShell on this machine blocks bare `python`).
- Existing behaviour for Amazon charges **below** the threshold must not change.

---

### Task 1: Threshold configuration and predicate

**Files:**
- Modify: `bot/config.py` (add `AmazonConfig`, wire into `Settings`)
- Modify: `bot/ingest.py` (add `LARGE_AMAZON_DEFAULT_CENTS` and `_is_large_amazon_charge`)
- Modify: `config.yaml.example`
- Test: `tests/test_large_amazon.py` (new)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `bot.config.AmazonConfig` with field `large_charge_cents: int = 15000`
  - `bot.config.Settings.amazon: AmazonConfig` (defaulted, so existing `config.yaml` files keep loading)
  - `bot.ingest.LARGE_AMAZON_DEFAULT_CENTS: int = 15000`
  - `bot.ingest._is_large_amazon_charge(amount_cents: int, settings: Settings | None) -> bool`

- [ ] **Step 1: Write the failing test**

Create `tests/test_large_amazon.py`:

```python
"""Large Amazon charges bypass the auto-bucket and ask instead.

Spec: docs/superpowers/specs/2026-07-25-large-amazon-attention-design.md
"""
import sqlite3
from datetime import date

from bot import ingest, storage


def test_threshold_defaults_to_15000_cents():
    assert ingest.LARGE_AMAZON_DEFAULT_CENTS == 15000


def test_charge_at_threshold_is_large():
    assert ingest._is_large_amazon_charge(-15000, None) is True


def test_charge_below_threshold_is_not_large():
    assert ingest._is_large_amazon_charge(-14999, None) is False


def test_refund_is_never_large():
    # Signed test, not abs() — an $815 refund must not raise a question.
    assert ingest._is_large_amazon_charge(81509, None) is False


def test_settings_override_threshold():
    from bot.config import AmazonConfig, Settings

    s = Settings.model_construct(amazon=AmazonConfig(large_charge_cents=50000))
    assert ingest._is_large_amazon_charge(-20000, s) is False
    assert ingest._is_large_amazon_charge(-50000, s) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_large_amazon.py -v`
Expected: FAIL with `AttributeError: module 'bot.ingest' has no attribute 'LARGE_AMAZON_DEFAULT_CENTS'`

- [ ] **Step 3: Add the config model**

In `bot/config.py`, add after the `OllamaConfig` class:

```python
class AmazonConfig(BaseModel):
    # Charges at or above this magnitude skip the per-person auto-bucket and
    # go through the confirm queue instead. ~6 charges a year at $150.
    large_charge_cents: int = 15000
```

Then add the field to `Settings` (after `ollama: OllamaConfig`):

```python
    amazon: AmazonConfig = AmazonConfig()
```

- [ ] **Step 4: Add the predicate**

In `bot/ingest.py`, add next to `_AMAZON_BUCKET_NAMES` (around line 912):

```python
# Amazon charges at or above this magnitude skip the auto-bucket entirely
# (spec 2026-07-25). Overridable via settings.amazon.large_charge_cents.
LARGE_AMAZON_DEFAULT_CENTS = 15000


def _is_large_amazon_charge(amount_cents: int, settings: Settings | None) -> bool:
    """True for an Amazon OUTFLOW at or above the large-charge threshold.

    Signed on purpose: a refund (positive amount) is never "large" no matter
    its size, because there is nothing to categorize — the original charge
    already carries the category.
    """
    threshold = LARGE_AMAZON_DEFAULT_CENTS
    if settings is not None:
        threshold = getattr(
            getattr(settings, "amazon", None), "large_charge_cents", threshold
        )
    return amount_cents <= -abs(threshold)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_large_amazon.py -v`
Expected: 5 passed

- [ ] **Step 6: Document the knob**

In `config.yaml.example`, add a top-level block near the `ollama:` block:

```yaml
amazon:
  # Charges at or above this many cents skip the per-person auto-bucket and
  # get asked about instead (~6 a year at 15000).
  large_charge_cents: 15000
```

- [ ] **Step 7: Verify the real config still loads**

Run: `.venv/Scripts/python.exe -c "from bot.config import load_settings; s=load_settings(); print(s.amazon.large_charge_cents)"`
Expected: `15000` (the live `config.yaml` has no `amazon:` block; the default must fill in)

- [ ] **Step 8: Commit**

```bash
git add bot/config.py bot/ingest.py config.yaml.example tests/test_large_amazon.py
git commit -m "feat(amazon): add large-charge threshold config and predicate"
```

---

### Task 2: Large charges skip the bucket and enter the HOLD lane

**Files:**
- Modify: `bot/ingest.py:216-233` (the auto-bucket fork), `bot/ingest.py:333-340` (the pending_txn gate), `bot/ingest.py:358-360` (the auto_commit flag)
- Test: `tests/test_large_amazon.py`

**Interfaces:**
- Consumes: `ingest._is_large_amazon_charge(amount_cents, settings)` from Task 1.
- Produces: after `ingest_signal()` on a large Amazon charge, `ledger_txn.category_id IS NULL` and a `pending_txn` row exists with `queue_lane = 'hold'` and `status = 'pending'`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_large_amazon.py`:

```python
ACCT = "acct-chase-1111"


def _setup(tmp_path):
    """Fresh DB with one account and the three Amazon buckets."""
    db = tmp_path / "test.db"
    storage.init_db(db)
    with storage.connect(db) as con:
        con.execute(
            "INSERT INTO account (id, name, type, on_budget, closed, last4) "
            "VALUES (?, 'Chase Amazon', 'credit_card', 1, 0, '1111')",
            (ACCT,),
        )
        con.execute(
            "INSERT INTO category_group (id, name) VALUES ('g1', 'Personal Spending')"
        )
        for cid, name in [
            ("cat-steven", "Amazon - Steven"),
            ("cat-allison", "Amazon - Allison"),
            ("cat-unassigned", "Amazon - Unassigned"),
        ]:
            con.execute(
                "INSERT INTO category (id, group_id, name, hidden, is_spending) "
                "VALUES (?, 'g1', ?, 0, 0)",
                (cid, name),
            )
    return db


def _charge(db, *, amount_cents, email_id, settings=None):
    return ingest.ingest_signal(
        db,
        signal_kind="chase_alert",
        email_id=email_id,
        parsed={
            "account_id": ACCT,
            "posted_date": date(2026, 7, 22),
            "amount_cents": amount_cents,
            "payee": "AMAZON MKTPLACE PMTS",
            "summary": "Chase $X at Amazon.com",
        },
        user_id="steven",
        settings=settings,
    )


def _row(db, table, rid):
    with storage.connect(db) as con:
        r = con.execute(f"SELECT * FROM {table} WHERE id = ?", (rid,)).fetchone()
    return dict(r) if r else None


def test_small_amazon_charge_still_auto_buckets(tmp_path):
    db = _setup(tmp_path)
    res = _charge(db, amount_cents=-4999, email_id="small-1")
    ledger = _row(db, "ledger_txn", res["ledger_txn_id"])
    assert ledger["category_id"] == "cat-unassigned"
    with storage.connect(db) as con:
        n = con.execute("SELECT COUNT(*) FROM pending_txn").fetchone()[0]
    assert n == 0, "small Amazon charges must not enter the confirm queue"


def test_large_amazon_charge_leaves_category_null(tmp_path):
    db = _setup(tmp_path)
    res = _charge(db, amount_cents=-81509, email_id="large-1")
    ledger = _row(db, "ledger_txn", res["ledger_txn_id"])
    assert ledger["category_id"] is None


def test_large_amazon_charge_enters_hold_lane(tmp_path):
    db = _setup(tmp_path)
    _charge(db, amount_cents=-81509, email_id="large-2")
    with storage.connect(db) as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM pending_txn")]
    assert len(rows) == 1
    assert rows[0]["queue_lane"] == "hold"
    assert rows[0]["status"] == "pending"


def test_large_amazon_does_not_auto_commit_from_matched_order(tmp_path):
    """A user-chosen category on the matching order normally auto-files.
    Above the threshold it must not — the whole point is a human look."""
    db = _setup(tmp_path)
    storage.insert_pending_order(
        db,
        user_id="steven", source="amazon", external_id="112-0031580-6551463",
        email_id="order-1", order_date=date(2026, 7, 22), total_cents=81509,
        raw_summary="1 item(s): 1 Electronics item", raw_payload={},
    )
    with storage.connect(db) as con:
        con.execute(
            "UPDATE pending_order SET chosen_category = 'cat-steven', "
            "assigned_to_user_id = 'steven' WHERE email_id = 'order-1'"
        )
    res = _charge(db, amount_cents=-81509, email_id="large-3")
    ledger = _row(db, "ledger_txn", res["ledger_txn_id"])
    assert ledger["category_id"] is None
    with storage.connect(db) as con:
        pt = con.execute("SELECT * FROM pending_txn").fetchone()
    assert pt["status"] == "pending"
    assert pt["chosen_category"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_large_amazon.py -v`
Expected: `test_small_amazon_charge_still_auto_buckets` PASSES (current behaviour); the three large-charge tests FAIL — `category_id` is `'cat-unassigned'` instead of `None` and no `pending_txn` row exists.

- [ ] **Step 3: Fork the auto-bucket branch**

In `bot/ingest.py`, replace line 187:

```python
    is_amazon = _is_amazon_payee(payee)
```

with:

```python
    is_amazon = _is_amazon_payee(payee)
    # Spec 2026-07-25: at or above the threshold an Amazon charge is too big
    # to file blind. It skips the bucket, holds uncategorized, and asks.
    is_large_amazon = is_amazon and _is_large_amazon_charge(amount_cents, settings)
```

Then change line 225 from:

```python
        if is_amazon:
```

to:

```python
        if is_amazon and not is_large_amazon:
```

- [ ] **Step 4: Let large charges into the confirm queue**

In `bot/ingest.py`, change the gate at line 334-335 from:

```python
    if (action == "new" and signal_kind in _PROMPT_USER_KINDS
            and user_id and not is_amazon):
```

to:

```python
    if (action == "new" and signal_kind in _PROMPT_USER_KINDS
            and user_id and (not is_amazon or is_large_amazon)):
```

- [ ] **Step 5: Suppress auto-commit above the threshold**

In `bot/ingest.py`, change line 359-360 from:

```python
                auto_commit = categorize_method in (
                    "override", "prior", "order", "trip")
```

to:

```python
                auto_commit = (
                    categorize_method in ("override", "prior", "order", "trip")
                    and not is_large_amazon
                )
```

- [ ] **Step 6: Put large charges in the HOLD lane**

In `bot/ingest.py`, immediately after the `insert_pending_txn` call assigns `pending_txn_id` (line 337-346) and before the lodging/trip block, insert:

```python
            if pending_txn_id and is_large_amazon:
                # Wait for the order email so the question can carry item
                # detail. queue_lane.promote_holds_to_hot() re-runs the
                # matcher every 30 min; a 24h TTL asks anyway if no receipt.
                from bot import queue_lane as _queue_lane
                _queue_lane.set_lane(
                    db_path, pending_txn_id, "hold",
                    reason="large_amazon_awaiting_receipt",
                )
                storage.audit(db_path, "large_amazon_held", {
                    "pending_txn_id": pending_txn_id,
                    "ledger_txn_id": ledger_txn_id,
                    "amount_cents": amount_cents, "payee": payee,
                })
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_large_amazon.py -v`
Expected: 9 passed

- [ ] **Step 8: Run the full suite for regressions**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: no new failures versus the pre-change baseline. Record the baseline first with `git stash` if unsure.

- [ ] **Step 9: Commit**

```bash
git add bot/ingest.py tests/test_large_amazon.py
git commit -m "feat(amazon): large charges hold for a receipt instead of auto-bucketing"
```

---

### Task 3: The retro-bucket path must not swallow held charges

**Files:**
- Modify: `bot/ingest.py:939-991` (`retro_bucket_amazon_order`)
- Test: `tests/test_large_amazon.py`

**Interfaces:**
- Consumes: `ingest._is_large_amazon_charge` from Task 1.
- Produces: `retro_bucket_amazon_order()` returns `False` and changes nothing when the only candidate charge is at or above the threshold.

Context: this function filed the real $815.09 into `Amazon - Steven` eight minutes after the charge landed (audit `amazon_retro_bucket`, 2026-07-23 02:54:39). Without this guard it will keep doing exactly that, undoing Task 2.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_large_amazon.py`:

```python
def test_retro_bucket_skips_large_charges(tmp_path):
    db = _setup(tmp_path)
    with storage.connect(db) as con:
        con.execute(
            "INSERT INTO ledger_txn (id, account_id, posted_date, amount_cents, "
            "payee, category_id, is_split) "
            "VALUES (900, ?, '2026-07-22', -81509, 'Amazon.com', "
            "'cat-unassigned', 0)",
            (ACCT,),
        )
    oid = storage.insert_pending_order(
        db,
        user_id="steven", source="amazon", external_id="112-0031580-6551463",
        email_id="order-large", order_date=date(2026, 7, 22), total_cents=81509,
        raw_summary="1 item(s): 1 Electronics item", raw_payload={},
    )
    with storage.connect(db) as con:
        con.execute(
            "UPDATE pending_order SET assigned_to_user_id = 'steven' WHERE id = ?",
            (oid,),
        )

    assert ingest.retro_bucket_amazon_order(db, order_id=oid) is False
    assert _row(db, "ledger_txn", 900)["category_id"] == "cat-unassigned"


def test_retro_bucket_still_works_for_small_charges(tmp_path):
    db = _setup(tmp_path)
    with storage.connect(db) as con:
        con.execute(
            "INSERT INTO ledger_txn (id, account_id, posted_date, amount_cents, "
            "payee, category_id, is_split) "
            "VALUES (901, ?, '2026-07-06', -13941, 'Amazon.com', "
            "'cat-unassigned', 0)",
            (ACCT,),
        )
    oid = storage.insert_pending_order(
        db,
        user_id="steven", source="amazon", external_id="112-4520723-6491412",
        email_id="order-small", order_date=date(2026, 7, 4), total_cents=13941,
        raw_summary='1 item(s): "TaylorMade Golf Milled..." and 2 more items',
        raw_payload={},
    )
    with storage.connect(db) as con:
        con.execute(
            "UPDATE pending_order SET assigned_to_user_id = 'steven' WHERE id = ?",
            (oid,),
        )

    assert ingest.retro_bucket_amazon_order(db, order_id=oid) is True
    assert _row(db, "ledger_txn", 901)["category_id"] == "cat-steven"
```

- [ ] **Step 2: Run tests to verify one fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_large_amazon.py -k retro -v`
Expected: `test_retro_bucket_still_works_for_small_charges` PASSES; `test_retro_bucket_skips_large_charges` FAILS (it returns `True` and rewrites the category to `cat-steven`).

- [ ] **Step 3: Add the guard**

In `bot/ingest.py` inside `retro_bucket_amazon_order`, after:

```python
    person = o["assigned_to_user_id"]
    total = o["total_cents"] or 0
    if not person or not total:
        return False
```

insert:

```python
    # Spec 2026-07-25: a charge at or above the threshold is deliberately
    # being held for a human decision. Re-bucketing it here is exactly the
    # behaviour that filed the real $815.09 eight minutes after it landed.
    if _is_large_amazon_charge(-abs(total), None):
        log.info("retro_bucket: order %s is $%.2f — above the large-charge "
                 "threshold, leaving it for the confirm queue",
                 order_id, abs(total) / 100)
        return False
```

Note: `settings` is not in scope here, so the module default applies. That is intentional — this path is a safety guard, and the default is the conservative value.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_large_amazon.py -k retro -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add bot/ingest.py tests/test_large_amazon.py
git commit -m "fix(amazon): retro-bucket skips charges held for a decision"
```

---

### Task 4: 24h TTL for large holds, and expiry asks instead of going cold

**Files:**
- Modify: `bot/queue_lane.py:47-54` (TTL constants), `bot/queue_lane.py:129-192` (`abandon_stale_holds`), `bot/queue_lane.py:267-275` (`sweep_lanes`)
- Test: `tests/test_large_amazon.py`

**Interfaces:**
- Consumes: nothing from prior tasks (operates on `pending_txn` rows directly).
- Produces:
  - `queue_lane.LARGE_AMAZON_HOLD_TTL_HOURS: int = 24`
  - `queue_lane.abandon_stale_holds(db_path, *, settings=None) -> int` — note the new keyword-only `settings` parameter, defaulted so existing callers keep working.

Context: today Amazon holds get a 14-day TTL and then drop to COLD with an `amazon_aged_out` audit event. Under this spec, small Amazon charges never enter HOLD at all, so `AMAZON_HOLD_TTL_DAYS` and that entire branch become unreachable — delete them. Large holds expire to **HOT** (ask anyway with amount and date), then follow the normal HOT→COLD path after 2h.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_large_amazon.py`:

```python
from bot import queue_lane


def _hold_row(db, *, amount_cents, hours_ago, payee="AMAZON MKTPLACE PMTS"):
    """Insert a pending_txn already sitting in HOLD, aged by hours_ago."""
    pt_id = storage.insert_pending_txn(
        db,
        user_id="steven",
        ynab_txn_id=f"ledger:{amount_cents}:{hours_ago}",
        ynab_account_id=ACCT,
        payee=payee,
        amount_cents=amount_cents,
        txn_date=date(2026, 7, 22),
        memo="",
    )
    with storage.connect(db) as con:
        con.execute(
            "UPDATE pending_txn SET queue_lane = 'hold', "
            "lane_changed_at = datetime('now', ?) WHERE id = ?",
            (f"-{hours_ago} hours", pt_id),
        )
    return pt_id


def test_large_hold_expires_to_hot_after_24h(tmp_path):
    db = _setup(tmp_path)
    pt_id = _hold_row(db, amount_cents=-81509, hours_ago=25)
    queue_lane.abandon_stale_holds(db)
    assert _row(db, "pending_txn", pt_id)["queue_lane"] == "hot", (
        "an expired large hold must ASK, not drop into the cold pile"
    )


def test_large_hold_waits_under_24h(tmp_path):
    db = _setup(tmp_path)
    pt_id = _hold_row(db, amount_cents=-81509, hours_ago=3)
    queue_lane.abandon_stale_holds(db)
    assert _row(db, "pending_txn", pt_id)["queue_lane"] == "hold"


def test_non_amazon_hold_still_goes_cold(tmp_path):
    db = _setup(tmp_path)
    pt_id = _hold_row(db, amount_cents=-2200, hours_ago=25, payee="APPLE.COM/BILL")
    queue_lane.abandon_stale_holds(db)
    assert _row(db, "pending_txn", pt_id)["queue_lane"] == "cold"


def test_amazon_14_day_ttl_is_gone(tmp_path):
    assert not hasattr(queue_lane, "AMAZON_HOLD_TTL_DAYS")
    assert queue_lane.LARGE_AMAZON_HOLD_TTL_HOURS == 24
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_large_amazon.py -k hold -v`
Expected: `test_large_hold_expires_to_hot_after_24h` FAILS (the row stays `hold` — the 14-day Amazon TTL hasn't elapsed), and `test_amazon_14_day_ttl_is_gone` FAILS.

- [ ] **Step 3: Replace the TTL constants**

In `bot/queue_lane.py`, replace lines 47-54:

```python
# Default HOLD TTL for non-Amazon items (Apple, Venmo). Kept short because
# their match windows are tight — Apple receipts arrive within hours.
HOLD_TTL_HOURS = 24
# Amazon receipts can arrive days to weeks after the CC charge (third-party
# sellers, slow shipments). Per Steven's directive (2026-06-26): never
# auto-process an Amazon item without enrichment; surface the unmatched
# ones as a dedicated alert when they exceed this window.
AMAZON_HOLD_TTL_DAYS = 14
```

with:

```python
# Default HOLD TTL for non-Amazon items (Apple, Venmo). Kept short because
# their match windows are tight — Apple receipts arrive within hours.
HOLD_TTL_HOURS = 24
# Large Amazon charges (spec 2026-07-25) wait this long for the order email
# so the question can carry item detail, then get asked anyway. Small Amazon
# charges never enter HOLD — they auto-bucket at ingest — so the old 14-day
# Amazon TTL and its aged-out alert are gone.
LARGE_AMAZON_HOLD_TTL_HOURS = 24
LARGE_AMAZON_THRESHOLD_CENTS = 15000
```

- [ ] **Step 4: Rewrite `abandon_stale_holds`**

In `bot/queue_lane.py`, replace the whole function body (lines 129-192) with:

```python
def abandon_stale_holds(db_path: Path | str, *, settings=None) -> int:
    """HOLD rows that exhaust their TTL move on.

      * Large Amazon — 24h, then promoted to HOT so the user is ASKED with
        whatever detail we have (amount + date). Spec 2026-07-25: an
        unanswered big charge must nag, not settle quietly into the cold pile.
      * Other — 24h, then COLD, unchanged.

    ``settings`` overrides the large-charge threshold when supplied; the
    module default applies otherwise.
    """
    threshold = LARGE_AMAZON_THRESHOLD_CENTS
    if settings is not None:
        threshold = getattr(
            getattr(settings, "amazon", None), "large_charge_cents", threshold
        )

    total = 0
    with storage.connect(db_path) as con:
        # Large Amazon — ask anyway.
        large_rows = con.execute(
            "SELECT id FROM pending_txn "
            "WHERE queue_lane = 'hold' AND status = 'pending' "
            "  AND (UPPER(payee) LIKE '%AMAZON%' OR UPPER(payee) LIKE '%AMZN%') "
            "  AND amount_cents <= ? "
            "  AND COALESCE(lane_changed_at, created_at) <= "
            "      datetime('now', ?)",
            (-abs(threshold), f"-{LARGE_AMAZON_HOLD_TTL_HOURS} hours"),
        ).fetchall()
        if large_rows:
            ids = [r["id"] for r in large_rows]
            placeholders = ",".join(["?"] * len(ids))
            con.execute(
                f"UPDATE pending_txn SET queue_lane = 'hot', "
                f"last_pushed_at = NULL, lane_changed_at = ? "
                f"WHERE id IN ({placeholders})",
                [_utcnow(), *ids],
            )
            storage.audit(db_path, "large_amazon_ask_unenriched", {
                "count": len(ids), "pt_ids": ids,
                "ttl_hours": LARGE_AMAZON_HOLD_TTL_HOURS,
            })
            log.warning("abandon_stale_holds: %d large Amazon items asked "
                        "without a receipt (>%dh)", len(ids),
                        LARGE_AMAZON_HOLD_TTL_HOURS)
            total += len(ids)

        # Everything else — COLD.
        other_rows = con.execute(
            "SELECT id FROM pending_txn "
            "WHERE queue_lane = 'hold' AND status = 'pending' "
            "  AND COALESCE(lane_changed_at, created_at) <= "
            "      datetime('now', ?)",
            (f"-{HOLD_TTL_HOURS} hours",),
        ).fetchall()
        if other_rows:
            ids = [r["id"] for r in other_rows]
            placeholders = ",".join(["?"] * len(ids))
            con.execute(
                f"UPDATE pending_txn SET queue_lane = 'cold', "
                f"lane_changed_at = ? WHERE id IN ({placeholders})",
                [_utcnow(), *ids],
            )
            storage.audit(db_path, "queue_lane_change", {
                "to": "cold", "from": "hold",
                "reason": "hold_ttl_expired", "count": len(ids),
            })
            log.info("abandon_stale_holds: %d rows to cold", len(ids))
            total += len(ids)
    return total
```

Note the large-Amazon query runs first and flips those rows to `hot`, so the second query — which no longer filters on payee — cannot pick them up (they are no longer `queue_lane = 'hold'`).

- [ ] **Step 5: Pass settings through the sweep**

In `bot/queue_lane.py`, change the `sweep_lanes` body to forward settings:

```python
        "abandoned_holds": abandon_stale_holds(db_path, settings=settings),
```

- [ ] **Step 6: Update the module docstring**

In `bot/queue_lane.py`, replace the `HOLD` line in the States block (line 7-8):

```
    HOLD  — Amazon CC alert without a matched order email yet.
            Sweep promotes to HOT on enrichment, or COLD after 24h.
```

with:

```
    HOLD  — a charge waiting on its receipt: a LARGE Amazon charge (>= the
            large-charge threshold) or an Apple/Venmo item. Sweep promotes to
            HOT on enrichment; large Amazon also promotes to HOT after 24h so
            it gets asked, everything else drops to COLD.
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_large_amazon.py -v`
Expected: all pass (15 tests)

- [ ] **Step 8: Verify nothing else referenced the deleted constant**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q` and `grep -rn "AMAZON_HOLD_TTL_DAYS\|amazon_aged_out" bot/ scripts/ tests/`

Expected from grep: hits only in `bot/amazon_tracker.py` (the dormant `/amazon` tracker, deliberately out of scope — it reads the audit event, and no new `amazon_aged_out` events will be written, so it will simply report nothing). No hits in `bot/queue_lane.py`.

- [ ] **Step 9: Commit**

```bash
git add bot/queue_lane.py tests/test_large_amazon.py
git commit -m "feat(amazon): 24h hold TTL for large charges, expiry asks instead of going cold"
```

---

### Task 5: Promotion adopts the order's suggested category

**Files:**
- Modify: `bot/queue_lane.py:195-264` (`promote_holds_to_hot`)
- Test: `tests/test_large_amazon.py`

**Interfaces:**
- Consumes: nothing from prior tasks.
- Produces: `promote_holds_to_hot()` sets `pending_txn.suggested_category` from the matched order's `chosen_category` when present, otherwise from its `suggested_category`.

Context: the real order 83 carried `suggested_category` at 0.6 confidence that never reached the user, because the promote path only reads `chosen_category`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_large_amazon.py`:

```python
def test_promotion_adopts_order_suggestion(tmp_path):
    db = _setup(tmp_path)
    pt_id = _hold_row(db, amount_cents=-81509, hours_ago=1)
    storage.insert_pending_order(
        db,
        user_id="steven", source="amazon", external_id="112-0031580-6551463",
        email_id="order-sugg", order_date=date(2026, 7, 22), total_cents=81509,
        raw_summary="1 item(s): 1 Electronics item", raw_payload={},
    )
    with storage.connect(db) as con:
        con.execute(
            "UPDATE pending_order SET suggested_category = 'cat-steven', "
            "assigned_to_user_id = 'steven' WHERE email_id = 'order-sugg'"
        )

    promoted = queue_lane.promote_holds_to_hot(db, settings=None)

    assert promoted == 1
    row = _row(db, "pending_txn", pt_id)
    assert row["queue_lane"] == "hot"
    assert row["suggested_category"] == "cat-steven"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_large_amazon.py::test_promotion_adopts_order_suggestion -v`
Expected: FAIL — `suggested_category` is `None` (the row is promoted to `hot`, but the suggestion is dropped).

- [ ] **Step 3: Adopt the suggestion**

In `bot/queue_lane.py` inside `promote_holds_to_hot`, replace:

```python
        new_cat = matched.get("chosen_category")
```

with:

```python
        # A confirmed user choice wins; otherwise carry the order's own
        # suggestion through so the prompt offers a starting guess instead
        # of a bare amount (order 83 carried a 0.6-confidence pick that
        # never reached the user).
        new_cat = matched.get("chosen_category") or matched.get(
            "suggested_category")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_large_amazon.py::test_promotion_adopts_order_suggestion -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: no new failures.

- [ ] **Step 6: Commit**

```bash
git add bot/queue_lane.py tests/test_large_amazon.py
git commit -m "fix(amazon): carry the order's suggested category into the prompt"
```

---

### Task 6: One-shot repair of the two existing over-threshold rows

**Files:**
- Create: `scripts/raise_large_amazon_charges.py`
- Test: manual dry-run against the live DB (this is a data-repair script, not library code)

**Interfaces:**
- Consumes: `ingest._is_large_amazon_charge`, `envelope.apply_activity_delta(db_path, month, category_ids)`.
- Produces: nothing other tasks depend on.

Context: ledger rows **24938** ($815.09, currently `Amazon - Steven`) and **24779** ($278.82, currently `Amazon - Unassigned`) predate the rule and must be raised for real categorization. Deliberately uses `envelope.apply_activity_delta` — documented in `bot/envelope.py:282` as "the anchor-preserving replacement for chain recompute after a past-month recategorization," which is exactly this case. **Never** `recompute_month`: it rebuilds `available` from the identity and trampled the anchor writes on 2026-07-25.

- [ ] **Step 1: Write the script**

Create `scripts/raise_large_amazon_charges.py`:

```python
"""Raise already-filed Amazon charges at/above the large-charge threshold.

Clears ledger_txn.category_id, enqueues a COLD pending_txn so the row shows
up in the Inbox, and refreshes ONLY the affected bucket categories' cached
month_category rows.

Uses apply_activity_delta (additive, anchor-preserving, scoped to the given
categories) not recompute_month — the latter rebuilds `available` from the
identity and trampled same-month anchor writes on 2026-07-25 (see
bot/envelope.py:301).

Usage:
    .venv/Scripts/python.exe scripts/raise_large_amazon_charges.py --dry-run
    .venv/Scripts/python.exe scripts/raise_large_amazon_charges.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-06-01",
                    help="only touch charges posted on/after this date")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from dotenv import load_dotenv
    repo = Path(__file__).resolve().parents[1]
    load_dotenv(repo / ".env", override=True)

    from bot import envelope, ingest, storage
    from bot.config import load_settings

    settings = load_settings(repo / "config.yaml")
    db = settings.paths.database

    with storage.connect(db) as con:
        rows = [dict(r) for r in con.execute(
            """SELECT lt.id, lt.posted_date, lt.payee, lt.amount_cents,
                      lt.account_id, lt.category_id, c.name AS cat_name
               FROM ledger_txn lt
               JOIN category c ON c.id = lt.category_id
               WHERE c.name LIKE 'Amazon - %'
                 AND lt.posted_date >= ?
                 AND lt.is_split = 0""",
            (args.since,),
        )]

    targets = [r for r in rows
               if ingest._is_large_amazon_charge(r["amount_cents"], settings)]
    if not targets:
        print("nothing to raise.")
        return 0

    touched_categories: set[tuple[str, str]] = set()
    for r in targets:
        month = str(r["posted_date"])[:7]
        print(f"  lt#{r['id']}  {r['posted_date']}  "
              f"${r['amount_cents'] / 100:>10,.2f}  {r['payee']}  "
              f"[{r['cat_name']}] -> Inbox")
        touched_categories.add((month, r["category_id"]))
        if args.dry_run:
            continue

        with storage.connect(db) as con:
            con.execute(
                "UPDATE ledger_txn SET category_id = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (r["id"],),
            )
        pt_id = storage.insert_pending_txn(
            db,
            user_id="steven",
            ynab_txn_id=f"ledger:{r['id']}",
            ynab_account_id=r["account_id"],
            payee=r["payee"],
            amount_cents=r["amount_cents"],
            txn_date=r["posted_date"],
            memo=f"raised for categorization (was {r['cat_name']})",
        )
        if pt_id:
            with storage.connect(db) as con:
                con.execute(
                    "UPDATE pending_txn SET queue_lane = 'cold' WHERE id = ?",
                    (pt_id,),
                )
        storage.audit(db, "large_amazon_raised", {
            "ledger_txn_id": r["id"], "pending_txn_id": pt_id,
            "was_category": r["cat_name"], "amount_cents": r["amount_cents"],
        })

    if args.dry_run:
        print(f"\n[dry-run] would raise {len(targets)} charge(s); "
              f"would refresh {len(touched_categories)} month/category pair(s).")
        return 0

    by_month: dict[str, list[str]] = {}
    for month, category_id in touched_categories:
        by_month.setdefault(month, []).append(category_id)
    for month, category_ids in sorted(by_month.items()):
        envelope.apply_activity_delta(db, month, category_ids)
        print(f"  refreshed {month}: {len(category_ids)} categor(ies)")

    print(f"\nraised {len(targets)} charge(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Confirm the helper signature before running**

Run: `grep -n "def apply_activity_delta" -A 10 bot/envelope.py`
Expected: `apply_activity_delta(db_path, month, category_ids: Iterable[str]) -> dict[str, int]`. If the real signature differs, fix the call site to match before continuing — do not guess.

- [ ] **Step 3: Dry-run against the live DB**

Run: `.venv/Scripts/python.exe scripts/raise_large_amazon_charges.py --dry-run`
Expected output: exactly two rows — `lt#24938` ($815.09, `Amazon - Steven`) and `lt#24779` ($278.82, `Amazon - Unassigned`) — and "would raise 2 charge(s)".

**If more than two rows appear, stop and report before running for real.**

- [ ] **Step 4: Run for real**

Run: `.venv/Scripts/python.exe scripts/raise_large_amazon_charges.py`
Expected: two charges raised, month/category pairs refreshed.

- [ ] **Step 5: Verify**

Run:

```bash
.venv/Scripts/python.exe -c "
import sqlite3
c = sqlite3.connect('ynab_helper.db'); c.row_factory = sqlite3.Row
for r in c.execute('''SELECT id, posted_date, payee, amount_cents, category_id
                      FROM ledger_txn WHERE id IN (24938, 24779)'''):
    print(dict(r))
for r in c.execute('''SELECT id, payee, amount_cents, queue_lane, status
                      FROM pending_txn WHERE ynab_txn_id IN
                      ('ledger:24938','ledger:24779')'''):
    print(dict(r))
"
```

Expected: both ledger rows have `category_id = None`; two `pending_txn` rows exist with `queue_lane='cold'` and `status='pending'`.

- [ ] **Step 6: Commit**

```bash
git add scripts/raise_large_amazon_charges.py
git commit -m "chore(amazon): one-shot raise of the two over-threshold bucket charges"
```

---

### Task 7: Deployment

**Files:** none.

- [ ] **Step 1: Full suite green**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: no new failures.

- [ ] **Step 2: Report, do not restart**

The ingest and queue-lane changes go live only when the `YNAB-Helper-Bot` scheduled task restarts. **Do not PID-kill the bot, do not start one in-session, and do not run the restart yourself** — a parallel agent may be mid-deploy. Report to Steven that the change is committed and awaiting a bot restart, and let him decide when (the Bot Control panel can do it).

- [ ] **Step 3: First-fire verification (after the restart, whenever it happens)**

Once a large Amazon charge next arrives, confirm the chain with:

```bash
.venv/Scripts/python.exe -c "
import sqlite3
c = sqlite3.connect('ynab_helper.db'); c.row_factory = sqlite3.Row
for r in c.execute('''SELECT ts, event, details FROM audit_log
                      WHERE event IN ('large_amazon_held',
                                      'large_amazon_ask_unenriched',
                                      'large_amazon_raised')
                      ORDER BY ts DESC LIMIT 10'''):
    print(r['ts'], r['event'], r['details'][:200])
"
```

Expected: a `large_amazon_held` event at ingest, and either a lane change to `hot` when the receipt lands or a `large_amazon_ask_unenriched` event ~24h later.

---

## Notes for the implementer

- **Do not touch `bot/amazon_tracker.py` or the `/amazon` command.** They go fully dark under this change (no new `amazon_aged_out` events will be written, so the tracker reports nothing). Retiring them is a separate decision already flagged in redesign-v2. Leaving dead-but-harmless code is the correct outcome here.
- **Do not widen the threshold to non-Amazon payees.** A generic large-purchase gate across every auto-file path is a known follow-on, deliberately out of scope. Note that the Business Fund investigation found an $8,201.11 charge auto-filed by a "strong prior" with no human review — that is the case for the follow-on, not a reason to expand this change.
- **Split shipments are a known gap.** A $600 order arriving as three $200 charges produces three questions, none matching the order. Accepted; do not build around it.
