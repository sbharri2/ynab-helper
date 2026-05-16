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
