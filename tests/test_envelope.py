"""Tests for bot/envelope.py — envelope budgeting math.

Each test builds a fabricated ledger_txn + month_category state directly via
storage.connect(), then verifies recompute / assign / move / roll_forward
arithmetic without involving YNAB or the LLM.
"""
from __future__ import annotations

from datetime import date

import pytest

from bot import storage
from bot.envelope import (
    assign_to_category,
    move_money,
    recompute_month,
    roll_forward,
)


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
