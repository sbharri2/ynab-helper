"""Sync must not revert a re-filing into a bot-only category.

Found 2026-07-26: the Exercise catch-all was split into per-person sport
categories (Steven Tennis, Allison Gym, ...), which are local-only —
created by the bot, so they carry no ynab_category_id. ynab_writer
therefore can never push them, and YNAB keeps reporting the pre-split
category. With "YNAB wins" as the blanket conflict rule, every sync
inside the rolling 7-day window silently re-filed those transactions back
to Exercise. Two golf charges were reverted overnight before this was
caught.
"""
from __future__ import annotations

import pytest

from bot import storage
from bot.ynab_full_sync import _upsert_ledger_txn


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "t.db"
    storage.init_db(p)
    with storage.connect(p) as con:
        con.execute(
            "INSERT INTO account (id, name, type, ynab_account_id) "
            "VALUES ('acc1', 'Card', 'credit_card', 'yacc1')"
        )
        con.execute(
            "INSERT INTO category_group (id, name) VALUES ('g1', 'Hobbies')")
        con.execute(
            "INSERT INTO category (id, group_id, name, ynab_category_id) "
            # Mirrored from YNAB — has a twin.
            "VALUES ('cat_exercise', 'g1', 'Exercise', 'ycat_exercise'), "
            "       ('cat_groc',     'g1', 'Groceries', 'ycat_groc'), "
            # Bot-invented — no YNAB twin, so unpushable.
            "       ('local-tennis', 'g1', 'Steven Tennis', NULL)"
        )
    return p


def _ytx(ycat: str | None):
    return {
        "ynab_txn_id": "ytxn1", "ynab_account_id": "yacc1",
        "txn_date": "2026-07-22", "amount_cents": -2000,
        "payee": "Knights Play", "memo": "", "cleared": "cleared",
        "ynab_category_id": ycat,
        "transfer_account_id": None, "transfer_transaction_id": None,
    }


def _category_of(db, ynab_txn_id="ytxn1"):
    with storage.connect(db) as con:
        return con.execute(
            "SELECT category_id FROM ledger_txn WHERE ynab_txn_id = ?",
            (ynab_txn_id,),
        ).fetchone()["category_id"]


def test_local_only_category_survives_sync(db):
    """The regression: YNAB still says Exercise, local says Steven Tennis."""
    _upsert_ledger_txn(db, ytx=_ytx("ycat_exercise"),
                       local_account_id="acc1",
                       local_category_id="cat_exercise")
    assert _category_of(db) == "cat_exercise"

    # The split re-files it into a bot-only category.
    with storage.connect(db) as con:
        con.execute("UPDATE ledger_txn SET category_id = 'local-tennis' "
                    "WHERE ynab_txn_id = 'ytxn1'")

    # Sync runs again; YNAB has not changed and still reports Exercise.
    _upsert_ledger_txn(db, ytx=_ytx("ycat_exercise"),
                       local_account_id="acc1",
                       local_category_id="cat_exercise")
    assert _category_of(db) == "local-tennis"


def test_ynab_still_wins_for_mirrored_categories(db):
    """The guard must stay narrow — a real YNAB re-categorization into a
    mirrored category is still authoritative."""
    _upsert_ledger_txn(db, ytx=_ytx("ycat_exercise"),
                       local_account_id="acc1",
                       local_category_id="cat_exercise")

    # Steven re-categorizes in YNAB itself: Exercise -> Groceries.
    _upsert_ledger_txn(db, ytx=_ytx("ycat_groc"),
                       local_account_id="acc1",
                       local_category_id="cat_groc")
    assert _category_of(db) == "cat_groc"


def test_null_ynab_category_still_keeps_local(db):
    """Pre-existing guard: YNAB null must never wipe a local decision."""
    _upsert_ledger_txn(db, ytx=_ytx("ycat_exercise"),
                       local_account_id="acc1",
                       local_category_id="cat_exercise")
    _upsert_ledger_txn(db, ytx=_ytx(None),
                       local_account_id="acc1",
                       local_category_id=None)
    assert _category_of(db) == "cat_exercise"
