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
