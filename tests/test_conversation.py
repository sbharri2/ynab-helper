from datetime import date
from bot import storage
from bot.conversation import next_item_for_user, format_item_prompt


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
