"""Tests for bot/envelope.py — envelope budgeting math.

Each test builds a fabricated ledger_txn + month_category state directly via
storage.connect(), then verifies recompute / assign / move / roll_forward
arithmetic without involving YNAB or the LLM.
"""
from __future__ import annotations

from datetime import date

import pytest

from fastapi.testclient import TestClient

from bot import storage
from bot.envelope import (
    assign_to_category,
    move_money,
    recompute_month,
    roll_forward,
)
from bot.http_api import build_app


def _seed(db_path):
    """Create the minimum schema rows: 1 account, 1 group, 2 categories."""
    storage.init_db(db_path)
    with storage.connect(db_path) as con:
        con.execute(
            "INSERT INTO account (id, name, type, on_budget) VALUES (?, ?, ?, ?)",
            ("acct-1", "Joint Checking", "checking", 1),
        )
        con.execute(
            "INSERT INTO category_group (id, name) VALUES (?, ?)",
            ("grp-day", "Day to Day Expenses"),
        )
        con.execute(
            "INSERT INTO category (id, group_id, name, is_spending) VALUES (?, ?, ?, ?)",
            ("cat-groc", "grp-day", "Groceries", 1),
        )
        con.execute(
            "INSERT INTO category (id, group_id, name, is_spending) VALUES (?, ?, ?, ?)",
            ("cat-dining", "grp-day", "Dining Out", 1),
        )


def _add_txn(db_path, *, account, category, posted_date, amount_cents,
             payee="merchant"):
    with storage.connect(db_path) as con:
        con.execute(
            """INSERT INTO ledger_txn
               (account_id, posted_date, amount_cents, payee, category_id, cleared)
               VALUES (?, ?, ?, ?, ?, 'cleared')""",
            (account, posted_date, amount_cents, payee, category),
        )


def test_recompute_month_sums_activity_correctly(tmp_path):
    db = tmp_path / "t.db"
    _seed(db)
    # Three Groceries charges in May 2026
    _add_txn(db, account="acct-1", category="cat-groc",
             posted_date=date(2026, 5, 3),  amount_cents=-5000)
    _add_txn(db, account="acct-1", category="cat-groc",
             posted_date=date(2026, 5, 17), amount_cents=-7300)
    _add_txn(db, account="acct-1", category="cat-groc",
             posted_date=date(2026, 5, 28), amount_cents=-2200)
    # One in April that shouldn't be picked up
    _add_txn(db, account="acct-1", category="cat-groc",
             posted_date=date(2026, 4, 30), amount_cents=-99999)

    results = recompute_month(db, "2026-05")

    # Groceries activity should be exactly the three May charges
    expected = -(5000 + 7300 + 2200)
    assert results["cat-groc"] == expected  # no prior, no budget → available = activity


def test_assign_to_category_increases_budgeted_and_available(tmp_path):
    db = tmp_path / "t.db"
    _seed(db)
    # Spend $50 on groceries this month
    _add_txn(db, account="acct-1", category="cat-groc",
             posted_date=date(2026, 5, 10), amount_cents=-5000)

    # Assign $200 to Groceries
    state = assign_to_category(db, "2026-05", "cat-groc", 20000)

    assert state["budgeted_cents"] == 20000
    assert state["activity_cents"] == -5000
    assert state["available_cents"] == 15000   # 0 prior + 20000 - 5000


def test_assign_negative_subtracts_from_budgeted(tmp_path):
    db = tmp_path / "t.db"
    _seed(db)
    assign_to_category(db, "2026-05", "cat-groc", 30000)
    state = assign_to_category(db, "2026-05", "cat-groc", -10000)
    assert state["budgeted_cents"] == 20000


def test_move_money_transfers_between_envelopes(tmp_path):
    db = tmp_path / "t.db"
    _seed(db)
    assign_to_category(db, "2026-05", "cat-groc", 50000)
    assign_to_category(db, "2026-05", "cat-dining", 10000)

    result = move_money(db, "2026-05", "cat-groc", "cat-dining", 8000)

    assert result["from"]["budgeted_cents"] == 42000
    assert result["from"]["available_cents"] == 42000
    assert result["to"]["budgeted_cents"] == 18000
    assert result["to"]["available_cents"] == 18000


def test_move_money_rejects_non_positive(tmp_path):
    db = tmp_path / "t.db"
    _seed(db)
    with pytest.raises(ValueError):
        move_money(db, "2026-05", "cat-groc", "cat-dining", 0)
    with pytest.raises(ValueError):
        move_money(db, "2026-05", "cat-groc", "cat-dining", -100)


def test_roll_forward_carries_available_into_next_month(tmp_path):
    db = tmp_path / "t.db"
    _seed(db)
    # May: assign $200, spend $50 → leftover $150
    assign_to_category(db, "2026-05", "cat-groc", 20000)
    _add_txn(db, account="acct-1", category="cat-groc",
             posted_date=date(2026, 5, 10), amount_cents=-5000)
    recompute_month(db, "2026-05")  # picks up the activity

    touched = roll_forward(db, "2026-05")
    assert touched >= 1

    with storage.connect(db) as con:
        june_groc = con.execute(
            "SELECT * FROM month_category WHERE month = ? AND category_id = ?",
            ("2026-06", "cat-groc"),
        ).fetchone()
    assert june_groc is not None
    # Carried May leftover ($150) into June, no new budget, no activity yet
    assert june_groc["available_cents"] == 15000
    assert june_groc["budgeted_cents"] == 0
    assert june_groc["activity_cents"] == 0


def test_recompute_uses_prior_available_for_carryover(tmp_path):
    db = tmp_path / "t.db"
    _seed(db)
    # Set up May with $100 leftover via assign + spend
    assign_to_category(db, "2026-05", "cat-groc", 20000)
    _add_txn(db, account="acct-1", category="cat-groc",
             posted_date=date(2026, 5, 1), amount_cents=-10000)
    recompute_month(db, "2026-05")

    # In June, $80 spent and $0 budgeted
    _add_txn(db, account="acct-1", category="cat-groc",
             posted_date=date(2026, 6, 5), amount_cents=-8000)
    result = recompute_month(db, "2026-06", category_ids=["cat-groc"])

    # June available = 10000 (May leftover) + 0 (budgeted) + (-8000) = 2000
    assert result["cat-groc"] == 2000


def test_idempotent_recompute(tmp_path):
    db = tmp_path / "t.db"
    _seed(db)
    assign_to_category(db, "2026-05", "cat-groc", 20000)
    _add_txn(db, account="acct-1", category="cat-groc",
             posted_date=date(2026, 5, 10), amount_cents=-5000)

    r1 = recompute_month(db, "2026-05")
    r2 = recompute_month(db, "2026-05")
    assert r1 == r2


def test_closed_account_spend_does_not_move_envelopes(tmp_path):
    """A CLOSED on-budget account's categorized rows must not become activity.

    Regression guard for the 2026-07-25 HSA history import. The closed HSA holds
    five years of medical spend that stays categorized so spending analytics can
    read it, but its cash is long gone: q_ready_to_assign computes
    rta = cash_cents - available_cents and cash_cents filters closed = 0. Counting
    a closed account's spend as activity would lower available_cents and conjure
    Ready-to-Assign out of cash that no longer exists.
    """
    db = tmp_path / "t.db"
    _seed(db)
    with storage.connect(db) as con:
        con.execute(
            "INSERT INTO account (id, name, type, on_budget, closed) "
            "VALUES (?, ?, ?, 1, 1)",
            ("acct-closed", "HSA (closed)", "checking"),
        )

    # Same category, same month: one live charge, one from the closed account.
    _add_txn(db, account="acct-1", category="cat-groc",
             posted_date=date(2026, 5, 4), amount_cents=-2500)
    _add_txn(db, account="acct-closed", category="cat-groc",
             posted_date=date(2026, 5, 6), amount_cents=-30395)

    results = recompute_month(db, "2026-05")

    # Only the live account's charge counts.
    assert results["cat-groc"] == -2500


# ── /envelope/return_to_rta (HTTP layer) ────────────────────────────────
#
# Regression coverage for the 2026-07-25 "Piano Lessons" bug: the move
# dialog's Ready-to-Assign leg used to call /budget/set with an absolute
# recomputed value, which /budget/set rejects once carryover drives the
# result negative (budgeted 0, available 15000, returning 149 -> -14900).
# /envelope/return_to_rta names that operation explicitly instead.


def _client(tmp_path):
    db = tmp_path / "t.db"
    _seed(db)
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    (token_dir / "ui_api_token.txt").write_text("testtoken", encoding="utf-8")
    app = build_app(db_path=db, token_dir=token_dir, webui_dir=tmp_path / "webui")
    c = TestClient(app)
    c.headers.update({"X-API-Token": "testtoken"})
    c.db = db
    return c


def _seed_carryover(db_path, *, month, budgeted_cents, available_cents):
    """Piano-Lessons-shaped state: money sitting in `available_cents` that
    isn't backed by this month's `budgeted_cents` (i.e. carryover)."""
    with storage.connect(db_path) as con:
        con.execute(
            """INSERT INTO month_category
               (month, category_id, budgeted_cents, activity_cents, available_cents)
               VALUES (?, ?, ?, 0, ?)""",
            (month, "cat-groc", budgeted_cents, available_cents),
        )


def test_return_to_rta_happy_path_drives_budgeted_negative(tmp_path):
    c = _client(tmp_path)
    _seed_carryover(c.db, month="2026-07", budgeted_cents=0, available_cents=15000)

    r = c.post("/envelope/return_to_rta", json={
        "month": "2026-07", "category_id": "cat-groc", "cents": 14900,
    })

    assert r.status_code == 200
    result = r.json()["result"]
    assert result["budgeted_cents"] == -14900
    assert result["available_cents"] == 100

    with storage.connect(c.db) as con:
        row = con.execute(
            "SELECT budgeted_cents, available_cents FROM month_category "
            "WHERE month = ? AND category_id = ?",
            ("2026-07", "cat-groc"),
        ).fetchone()
    assert row["budgeted_cents"] == -14900
    assert row["available_cents"] == 100


def test_return_to_rta_rejects_cents_exceeding_available(tmp_path):
    c = _client(tmp_path)
    _seed_carryover(c.db, month="2026-07", budgeted_cents=0, available_cents=15000)

    r = c.post("/envelope/return_to_rta", json={
        "month": "2026-07", "category_id": "cat-groc", "cents": 15100,
    })

    assert r.status_code == 400
    assert "15100" in r.text
    assert "15000" in r.text

    # Nothing was written.
    with storage.connect(c.db) as con:
        row = con.execute(
            "SELECT budgeted_cents, available_cents FROM month_category "
            "WHERE month = ? AND category_id = ?",
            ("2026-07", "cat-groc"),
        ).fetchone()
    assert row["budgeted_cents"] == 0
    assert row["available_cents"] == 15000


@pytest.mark.parametrize("cents", [0, -100])
def test_return_to_rta_rejects_non_positive_cents(tmp_path, cents):
    c = _client(tmp_path)
    _seed_carryover(c.db, month="2026-07", budgeted_cents=0, available_cents=15000)

    r = c.post("/envelope/return_to_rta", json={
        "month": "2026-07", "category_id": "cat-groc", "cents": cents,
    })

    assert r.status_code == 400


def test_return_to_rta_rejects_bad_month(tmp_path):
    c = _client(tmp_path)
    _seed_carryover(c.db, month="2026-07", budgeted_cents=0, available_cents=15000)

    r = c.post("/envelope/return_to_rta", json={
        "month": "2026/07", "category_id": "cat-groc", "cents": 100,
    })

    assert r.status_code == 400


def test_return_to_rta_with_no_carryover_row_rejects_any_positive_cents(tmp_path):
    """No month_category row at all means available_cents is implicitly 0
    (nothing has ever been assigned or carried into this category-month) —
    returning anything must 400, not treat missing-row as unlimited."""
    c = _client(tmp_path)

    r = c.post("/envelope/return_to_rta", json={
        "month": "2026-07", "category_id": "cat-groc", "cents": 100,
    })

    assert r.status_code == 400
