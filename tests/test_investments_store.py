import sqlite3
from datetime import date

import pytest

from bot.storage import init_db, connect


INVESTMENT_TABLES = {
    "holding",
    "snapshot_round",
    "holding_value",
    "property_detail",
    "insurance_policy",
    "insurance_premium_observed",
    "savings_target",
}


def test_init_creates_investment_tables(tmp_path):
    db = tmp_path / "test.db"
    init_db(db)
    con = sqlite3.connect(db)
    tables = {
        r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert INVESTMENT_TABLES.issubset(tables)


def test_holding_value_unique_per_holding_round(tmp_path):
    db = tmp_path / "test.db"
    init_db(db)
    with connect(db) as con:
        con.execute(
            "INSERT INTO holding (id, name, kind) VALUES ('h1', 'Marcus', 'cash')"
        )
        con.execute(
            "INSERT INTO snapshot_round (id, label, as_of_date) "
            "VALUES ('r1', 'Jul 2026', '2026-07-25')"
        )
        con.execute(
            "INSERT INTO holding_value (holding_id, round_id, as_of_date, value_cents) "
            "VALUES ('h1', 'r1', '2026-07-25', 2566800)"
        )
    with pytest.raises(sqlite3.IntegrityError):
        with connect(db) as con:
            con.execute(
                "INSERT INTO holding_value (holding_id, round_id, as_of_date, value_cents) "
                "VALUES ('h1', 'r1', '2026-07-25', 999)"
            )


from bot import investments_store as store


def _seed_holdings(db):
    a = store.upsert_holding(db, name="Marcus", kind="cash", owner="joint")
    b = store.upsert_holding(db, name="Roth IRA", kind="retirement", owner="steven")
    return a, b


def test_create_round_returns_id_and_lists(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    rid = store.create_round(db, label="Jul 2026", as_of_date="2026-07-25")
    rounds = store.list_rounds(db)
    assert len(rounds) == 1
    assert rounds[0]["id"] == rid
    assert rounds[0]["label"] == "Jul 2026"
    assert rounds[0]["as_of_date"] == date(2026, 7, 25)
    assert rounds[0]["value_count"] == 0


def test_seed_from_previous_carries_values_and_marks_them(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    a, b = _seed_holdings(db)
    r1 = store.create_round(db, label="Feb 2026", as_of_date="2026-02-15")
    store.upsert_values(db, round_id=r1, values=[
        {"holding_id": a, "value_cents": 1000, "as_of_date": "2026-02-15"},
        {"holding_id": b, "value_cents": 2000, "as_of_date": "2026-02-15"},
    ])
    r2 = store.create_round(
        db, label="Jul 2026", as_of_date="2026-07-25", seed_from_previous=True,
    )
    with connect(db) as con:
        rows = {
            r["holding_id"]: dict(r) for r in con.execute(
                "SELECT * FROM holding_value WHERE round_id = ?", (r2,)
            )
        }
    assert rows[a]["value_cents"] == 1000
    assert rows[a]["is_seeded"] == 1
    assert rows[a]["as_of_date"] == date(2026, 7, 25)   # round date, not carried
    assert rows[b]["value_cents"] == 2000


def test_upsert_values_updates_and_clears_seeded_flag(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    a, _ = _seed_holdings(db)
    r1 = store.create_round(db, label="Feb 2026", as_of_date="2026-02-15")
    store.upsert_values(db, round_id=r1, values=[
        {"holding_id": a, "value_cents": 1000, "as_of_date": "2026-02-15"},
    ])
    r2 = store.create_round(
        db, label="Jul 2026", as_of_date="2026-07-25", seed_from_previous=True,
    )
    n = store.upsert_values(db, round_id=r2, values=[
        {"holding_id": a, "value_cents": 2566800, "as_of_date": "2026-07-20"},
    ])
    assert n == 1
    with connect(db) as con:
        row = dict(con.execute(
            "SELECT * FROM holding_value WHERE round_id = ? AND holding_id = ?",
            (r2, a),
        ).fetchone())
    assert row["value_cents"] == 2566800
    assert row["is_seeded"] == 0
    assert row["as_of_date"] == date(2026, 7, 20)
    with connect(db) as con:
        count = con.execute(
            "SELECT COUNT(*) FROM holding_value WHERE round_id = ?", (r2,)
        ).fetchone()[0]
    assert count == 1   # upsert, not a second row


def test_upsert_values_stores_components(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    p = store.upsert_holding(db, name="117 Mayfield", kind="property")
    c = store.upsert_holding(db, name="Bitcoin", kind="crypto")
    r = store.create_round(db, label="Jul 2026", as_of_date="2026-07-25")
    store.upsert_values(db, round_id=r, values=[
        {"holding_id": p, "value_cents": 24952292,
         "market_value_cents": 48240000, "debt_cents": 23287708},
        {"holding_id": c, "value_cents": 6072700,
         "units": 0.947, "unit_price_cents": 6411892},
    ])
    with connect(db) as con:
        rows = {
            r_["holding_id"]: dict(r_) for r_ in con.execute(
                "SELECT * FROM holding_value WHERE round_id = ?", (r,)
            )
        }
    assert rows[p]["market_value_cents"] - rows[p]["debt_cents"] == rows[p]["value_cents"]
    assert rows[c]["units"] == 0.947


def test_upsert_holding_updates_when_id_given(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    hid = store.upsert_holding(db, name="Old Name", kind="cash")
    same = store.upsert_holding(db, id=hid, name="New Name", kind="cash", closed=1)
    assert same == hid
    rows = store.list_holdings(db, include_closed=True)
    assert len(rows) == 1
    assert rows[0]["name"] == "New Name"
    assert rows[0]["closed"] == 1
    assert store.list_holdings(db, include_closed=False) == []
