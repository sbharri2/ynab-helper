from datetime import date
from bot import storage
from bot.conversation import next_item_for_user, format_item_prompt


def _insert_pending_txn(db, *, user_id="steven", ynab_txn_id="t1",
                        suggested_category="cat-x"):
    storage.insert_pending_txn(
        db, user_id=user_id, ynab_txn_id=ynab_txn_id,
        ynab_account_id="acct-1", payee="Starbucks",
        amount_cents=-450, txn_date=date(2026, 5, 15), memo="",
    )
    if suggested_category is not None:
        with storage.connect(db) as con:
            con.execute(
                "UPDATE pending_txn SET suggested_category = ? "
                "WHERE ynab_txn_id = ?",
                (suggested_category, ynab_txn_id),
            )


def test_pending_txn_without_suggestion_is_not_returned(tmp_path):
    """Catchup-gating: a pending_txn with no suggested_category should
    not surface to the bot. It must be enriched by daily_catchup first."""
    db = tmp_path / "test.db"
    storage.init_db(db)
    _insert_pending_txn(db, suggested_category=None)
    assert next_item_for_user(db, user_id="steven") is None


def test_next_item_returns_oldest_pending(tmp_path):
    db = tmp_path / "test.db"
    storage.init_db(db)
    storage.insert_pending_order(
        db, user_id="steven", source="amazon", external_id="A",
        email_id="m1", order_date=date(2026, 5, 12), total_cents=4723,
        raw_summary="diapers", raw_payload={"summary": "diapers", "items": ["Diapers"]},
    )
    storage.insert_pending_order(
        db, user_id="steven", source="amazon", external_id="B",
        email_id="m2", order_date=date(2026, 5, 13), total_cents=2200,
        raw_summary="batteries", raw_payload={"summary": "batteries", "items": ["AA"]},
    )

    item = next_item_for_user(db, user_id="steven")
    assert item is not None
    assert item["kind"] == "order"
    assert item["raw_summary"] == "diapers"


def test_next_item_skips_txns_when_include_txns_false(tmp_path):
    """Digest-gating: pending_txn must not surface when include_txns=False."""
    db = tmp_path / "test.db"
    storage.init_db(db)
    _insert_pending_txn(db)

    assert next_item_for_user(db, user_id="steven", include_txns=False) is None


def test_next_item_returns_txn_when_include_txns_true(tmp_path):
    """Same setup with include_txns=True returns the pending_txn."""
    db = tmp_path / "test.db"
    storage.init_db(db)
    _insert_pending_txn(db)

    item = next_item_for_user(db, user_id="steven", include_txns=True)
    assert item is not None
    assert item["kind"] == "txn"
    assert item["payee"] == "Starbucks"


def test_next_item_default_includes_txns(tmp_path):
    """Backward-compat: default behavior surfaces pending_txns."""
    db = tmp_path / "test.db"
    storage.init_db(db)
    _insert_pending_txn(db)

    item = next_item_for_user(db, user_id="steven")
    assert item is not None
    assert item["kind"] == "txn"


def test_next_item_orders_outrank_txns_even_when_include_txns_false(tmp_path):
    """Orders are always eligible — include_txns=False shouldn't hide them."""
    db = tmp_path / "test.db"
    storage.init_db(db)
    storage.insert_pending_order(
        db, user_id="steven", source="amazon", external_id="A",
        email_id="m1", order_date=date(2026, 5, 12), total_cents=4723,
        raw_summary="diapers", raw_payload={"summary": "diapers"},
    )
    _insert_pending_txn(db)

    item = next_item_for_user(db, user_id="steven", include_txns=False)
    assert item is not None
    assert item["kind"] == "order"


def test_format_item_prompt_amazon():
    item = {
        "kind": "order", "source": "amazon",
        "total_cents": 4723, "order_date": date(2026, 5, 12),
        "raw_summary": "3 items: Diapers, Wipes, Formula",
        "suggested_category_name": "Baby Supplies",
    }
    msg = format_item_prompt(item)
    assert "$47.23" in msg
    assert "Amazon" in msg
    assert "Baby Supplies" in msg
