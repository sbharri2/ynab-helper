"""Splits: YNAB split transactions mirror into child ledger rows so the
parent stops looking 'uncategorized' and the spend lands in per-category
activity. Guards the money-math invariant (parent == sum of children) and
the backlog exclusion.
"""
from __future__ import annotations

from datetime import date

import pytest

from bot import storage
from bot.ynab_full_sync import _sync_split_children, _upsert_ledger_txn


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "t.db"
    storage.init_db(p)
    with storage.connect(p) as con:
        con.execute(
            "INSERT INTO account (id, name, type, ynab_account_id) "
            "VALUES ('acc1', 'Card', 'credit_card', 'yacc1')"
        )
        con.execute("INSERT INTO category_group (id, name) VALUES ('g1', 'Day to Day Expenses')")
        con.execute(
            "INSERT INTO category (id, group_id, name, ynab_category_id) "
            "VALUES ('cat_groc', 'g1', 'Groceries', 'ycat_groc'), "
            "       ('cat_home', 'g1', 'Household', 'ycat_home')"
        )
    return p


def _split_ytx(sub_amounts):
    """A split parent ytx whose children sum to the parent total."""
    subs = []
    for i, (yid, ycat, amt) in enumerate(sub_amounts):
        subs.append({
            "ynab_txn_id": yid, "ynab_category_id": ycat,
            "transfer_account_id": None, "transfer_transaction_id": None,
            "payee": "Target", "amount_cents": amt, "memo": "",
        })
    total = sum(a for _, _, a in sub_amounts)
    return {
        "ynab_txn_id": "ytxn_parent", "ynab_account_id": "yacc1",
        "ynab_category_id": "ysplit", "transfer_account_id": None,
        "transfer_transaction_id": None, "payee": "Target",
        "amount_cents": total, "txn_date": date(2026, 5, 10), "memo": "",
        "cleared": "cleared", "approved": True, "subtransactions": subs,
    }


def _import_split(db, ytx):
    action, pid = _upsert_ledger_txn(
        db, ytx=ytx, local_account_id="acc1",
        local_category_id=None, is_split=bool(ytx["subtransactions"]),
    )
    _sync_split_children(db, parent_local_id=pid, ytx=ytx, local_account_id="acc1")
    return pid


def test_split_creates_children_and_flags_parent(db):
    pid = _import_split(db, _split_ytx([
        ("sub_a", "ycat_groc", -3000),
        ("sub_b", "ycat_home", -1500),
    ]))
    with storage.connect(db) as con:
        parent = con.execute(
            "SELECT amount_cents, category_id, is_split FROM ledger_txn WHERE id=?",
            (pid,)).fetchone()
        kids = con.execute(
            "SELECT category_id, amount_cents FROM ledger_txn "
            "WHERE parent_txn_id=? ORDER BY amount_cents", (pid,)).fetchall()
    assert parent["is_split"] == 1
    assert parent["category_id"] is None
    assert parent["amount_cents"] == -4500
    assert len(kids) == 2
    # Children carry real categories and sum back to the parent.
    assert {k["category_id"] for k in kids} == {"cat_groc", "cat_home"}
    assert sum(k["amount_cents"] for k in kids) == parent["amount_cents"]


def test_split_parent_excluded_from_backlog(db):
    _import_split(db, _split_ytx([
        ("sub_a", "ycat_groc", -3000),
        ("sub_b", "ycat_home", -1500),
    ]))
    with storage.connect(db) as con:
        # The UI backlog predicate (category NULL, outflow, is_split=0).
        backlog = con.execute(
            "SELECT COUNT(*) FROM ledger_txn WHERE category_id IS NULL "
            "AND amount_cents < 0 AND is_split = 0"
        ).fetchone()[0]
        # Per-category activity sees the children.
        groc = con.execute(
            "SELECT COALESCE(SUM(amount_cents),0) FROM ledger_txn WHERE category_id='cat_groc'"
        ).fetchone()[0]
    assert backlog == 0          # the split parent no longer pollutes it
    assert groc == -3000         # spend attributed to the child's category


def test_resync_is_idempotent_and_prunes_removed_legs(db):
    ytx = _split_ytx([
        ("sub_a", "ycat_groc", -3000),
        ("sub_b", "ycat_home", -1500),
    ])
    pid = _import_split(db, ytx)
    # Re-run with the same data: no duplicate children.
    _sync_split_children(db, parent_local_id=pid, ytx=ytx, local_account_id="acc1")
    with storage.connect(db) as con:
        n = con.execute(
            "SELECT COUNT(*) FROM ledger_txn WHERE parent_txn_id=?", (pid,)).fetchone()[0]
    assert n == 2
    # Drop a leg in YNAB → it gets pruned locally.
    ytx["subtransactions"] = ytx["subtransactions"][:1]
    _sync_split_children(db, parent_local_id=pid, ytx=ytx, local_account_id="acc1")
    with storage.connect(db) as con:
        rows = con.execute(
            "SELECT ynab_txn_id FROM ledger_txn WHERE parent_txn_id=?", (pid,)).fetchall()
    assert [r["ynab_txn_id"] for r in rows] == ["sub_a"]
