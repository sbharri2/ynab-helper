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
    n, changes = store.upsert_values(db, round_id=r2, values=[
        {"holding_id": a, "value_cents": 2566800, "as_of_date": "2026-07-20"},
    ])
    assert n == 1
    assert changes == [{"holding_id": a, "from_cents": 1000, "to_cents": 2566800}]
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


def test_upsert_values_partial_save_preserves_untouched_components(tmp_path):
    """Regression for the every-Save-nulls-everything bug.

    The editor only ever sends the fields the user actually touched — a
    plain "value" save with the breakout panel collapsed sends no
    market_value_cents/debt_cents/note at all. Those columns must survive
    untouched, not get nulled by the ON CONFLICT DO UPDATE.
    """
    db = tmp_path / "t.db"
    init_db(db)
    p = store.upsert_holding(db, name="117 Mayfield", kind="property")
    r = store.create_round(db, label="Jul 2026", as_of_date="2026-07-25")
    store.upsert_values(db, round_id=r, values=[
        {"holding_id": p, "value_cents": 24952292,
         "market_value_cents": 48240000, "debt_cents": 23287708,
         "note": "county assessment"},
    ])
    # Second save: only value_cents (and the implicit as_of_date fallback)
    # — the shape a plain "Save round" click produces when the breakout
    # panel was never expanded.
    store.upsert_values(db, round_id=r, values=[
        {"holding_id": p, "value_cents": 24952292},
    ])
    with connect(db) as con:
        row = dict(con.execute(
            "SELECT * FROM holding_value WHERE holding_id = ? AND round_id = ?",
            (p, r),
        ).fetchone())
    assert row["market_value_cents"] == 48240000
    assert row["debt_cents"] == 23287708
    assert row["note"] == "county assessment"


def test_upsert_values_explicit_null_clears_a_component(tmp_path):
    """Absent and explicitly-null are different: a key present with an
    explicit None is a deliberate clear and must still write NULL."""
    db = tmp_path / "t.db"
    init_db(db)
    p = store.upsert_holding(db, name="117 Mayfield", kind="property")
    r = store.create_round(db, label="Jul 2026", as_of_date="2026-07-25")
    store.upsert_values(db, round_id=r, values=[
        {"holding_id": p, "value_cents": 24952292, "note": "county assessment"},
    ])
    store.upsert_values(db, round_id=r, values=[
        {"holding_id": p, "value_cents": 24952292, "note": None},
    ])
    with connect(db) as con:
        row = dict(con.execute(
            "SELECT * FROM holding_value WHERE holding_id = ? AND round_id = ?",
            (p, r),
        ).fetchone())
    assert row["note"] is None


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


def _round_with(db, label, as_of, entries):
    rid = store.create_round(db, label=label, as_of_date=as_of)
    store.upsert_values(db, round_id=rid, values=entries)
    return rid


def test_minus_home_equity_subtracts_only_primary_residence(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    home = store.upsert_holding(db, name="117 Mayfield", kind="property")
    rental = store.upsert_holding(db, name="105 7th Ave", kind="property")
    cash = store.upsert_holding(db, name="Marcus", kind="cash")
    with connect(db) as con:
        con.execute(
            "INSERT INTO property_detail (holding_id, is_primary_residence) "
            "VALUES (?, 1)", (home,),
        )
        con.execute(
            "INSERT INTO property_detail (holding_id, is_primary_residence) "
            "VALUES (?, 0)", (rental,),
        )
    _round_with(db, "Jul 2026", "2026-07-25", [
        {"holding_id": home, "value_cents": 24952292},
        {"holding_id": rental, "value_cents": 6351669},
        {"holding_id": cash, "value_cents": 2566800},
    ])
    totals = {t["label"]: t["cells"] for t in store.compute_totals(db)}
    assert totals["Total"][0] == 24952292 + 6351669 + 2566800
    # rental stays in; only the primary residence comes out
    assert totals["Minus Home Equity"][0] == 6351669 + 2566800


def test_minus_home_equity_is_none_with_zero_primary_residences(tmp_path):
    """Zero flags must not silently collapse Minus Home Equity to Total —
    that overstates retirement readiness and flips Delta positive."""
    db = tmp_path / "t.db"
    init_db(db)
    cash = store.upsert_holding(db, name="Marcus", kind="cash")
    _round_with(db, "Jul 2026", "2026-07-25", [
        {"holding_id": cash, "value_cents": 2566800},
    ])
    totals = {t["label"]: t["cells"] for t in store.compute_totals(db)}
    assert totals["Minus Home Equity"][0] is None
    assert totals["Delta"][0] is None


def test_minus_home_equity_is_none_with_two_primary_residences(tmp_path):
    """Two flags must not silently double-subtract."""
    db = tmp_path / "t.db"
    init_db(db)
    home1 = store.upsert_holding(db, name="117 Mayfield", kind="property")
    home2 = store.upsert_holding(db, name="200 Elm St", kind="property")
    with connect(db) as con:
        con.execute(
            "INSERT INTO property_detail (holding_id, is_primary_residence) "
            "VALUES (?, 1)", (home1,),
        )
        con.execute(
            "INSERT INTO property_detail (holding_id, is_primary_residence) "
            "VALUES (?, 1)", (home2,),
        )
    _round_with(db, "Jul 2026", "2026-07-25", [
        {"holding_id": home1, "value_cents": 24952292},
        {"holding_id": home2, "value_cents": 6351669},
    ])
    totals = {t["label"]: t["cells"] for t in store.compute_totals(db)}
    assert totals["Minus Home Equity"][0] is None


def test_annual_change_annualizes_by_days(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    h = store.upsert_holding(db, name="Fund", kind="brokerage")
    _round_with(db, "A", "2025-07-25", [{"holding_id": h, "value_cents": 100000}])
    _round_with(db, "B", "2026-07-25", [{"holding_id": h, "value_cents": 200000}])
    totals = {t["label"]: t["cells"] for t in store.compute_totals(db)}
    assert totals["Annual Change"][0] is None          # no prior round
    assert totals["Annual Change"][1] == pytest.approx(100.0, abs=0.5)


def test_annual_change_is_none_when_round_total_goes_negative(tmp_path):
    """A leveraged property can push a round's total negative.

    growth ** (365/days) on a negative base is a complex number, which
    round() rejects with TypeError. compute_totals must not crash.
    """
    db = tmp_path / "t.db"
    init_db(db)
    h = store.upsert_holding(db, name="105 7th Ave", kind="property")
    # A non-exact-year gap gives a fractional exponent (365/days != 1),
    # which is what turns a negative base into a complex number.
    _round_with(db, "A", "2025-07-25", [{"holding_id": h, "value_cents": 100000}])
    _round_with(db, "B", "2026-08-01", [{"holding_id": h, "value_cents": -50000}])
    totals = {t["label"]: t["cells"] for t in store.compute_totals(db)}
    assert totals["Annual Change"][1] is None


def test_target_and_delta_use_savings_target_row(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    h = store.upsert_holding(db, name="Fund", kind="brokerage")
    # compute_totals only computes Minus Home Equity (and therefore Delta)
    # when exactly one holding is flagged as the primary residence — a $0
    # placeholder here keeps this test about target/delta math, not that
    # guard (covered separately by test_minus_home_equity_* and
    # test_delta_is_none_without_exactly_one_primary_residence).
    home = store.upsert_holding(db, name="117 Mayfield", kind="property")
    with connect(db) as con:
        con.execute(
            "INSERT INTO property_detail (holding_id, is_primary_residence) "
            "VALUES (?, 1)", (home,),
        )
    _round_with(db, "Jul 2026", "2026-07-25", [
        {"holding_id": h, "value_cents": 100000000},
        {"holding_id": home, "value_cents": 0},
    ])
    with connect(db) as con:
        con.execute(
            "INSERT INTO savings_target "
            "(id, effective_year, age, combined_salary_cents, multiplier) "
            "VALUES ('t1', 2026, 42, 29800000, 3.0)"
        )
    totals = {t["label"]: t["cells"] for t in store.compute_totals(db)}
    assert totals["Target Savings"][0] == 89400000
    assert totals["Delta"][0] == 100000000 - 89400000


def test_target_steps_to_4x_at_45(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    h = store.upsert_holding(db, name="Fund", kind="brokerage")
    _round_with(db, "Jul 2029", "2029-07-25", [
        {"holding_id": h, "value_cents": 1},
    ])
    with connect(db) as con:
        con.execute(
            "INSERT INTO savings_target "
            "(id, effective_year, age, combined_salary_cents, multiplier) "
            "VALUES ('t1', 2026, 42, 29800000, 3.0)"
        )
        con.execute(
            "INSERT INTO savings_target "
            "(id, effective_year, age, combined_salary_cents, multiplier) "
            "VALUES ('t2', 2029, 45, 29800000, 4.0)"
        )
    totals = {t["label"]: t["cells"] for t in store.compute_totals(db)}
    assert totals["Target Savings"][0] == 119200000


def test_build_snapshot_matches_typescript_shape(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    home = store.upsert_holding(
        db, name="117 Mayfield", kind="property", account_type="Home Equity",
        owner="joint", account_number="", notes="Zestimate",
    )
    fund = store.upsert_holding(
        db, name="Roth IRA", kind="retirement", account_type="Roth IRA",
        owner="steven", account_number="1234",
    )
    _round_with(db, "Feb 2026", "2026-02-15", [
        {"holding_id": home, "value_cents": 100},
        {"holding_id": fund, "value_cents": 200},
    ])
    _round_with(db, "Jul 2026", "2026-07-25", [
        {"holding_id": home, "value_cents": 300},
        {"holding_id": fund, "value_cents": 400},
    ])
    snap = store.build_snapshot(db)
    assert set(snap) == {
        "source_file", "as_of", "holdings", "insurance", "totals_rows",
    }
    assert snap["as_of"] == "2026-07-25"
    assert snap["source_file"] == "database"
    by_name = {h["name"]: h for h in snap["holdings"]}
    assert set(by_name) == {"117 Mayfield", "Roth IRA"}
    assert by_name["117 Mayfield"]["is_real_estate"] is True
    assert by_name["Roth IRA"]["is_real_estate"] is False
    assert by_name["Roth IRA"]["account_type"] == "Roth IRA"
    # oldest -> newest, one cell per round, never compressed
    assert [v["cents"] for v in by_name["Roth IRA"]["values"]] == [200, 400]
    assert [v["snapshot_date"] for v in by_name["Roth IRA"]["values"]] == [
        "2026-02-15", "2026-07-25",
    ]
    assert [v["label"] for v in by_name["Roth IRA"]["values"]] == ["Feb 2026", "Jul 2026"]
    total = next(t for t in snap["totals_rows"] if t["label"] == "Total")
    assert total["cells"] == [300, 700]


def test_build_snapshot_carries_is_seeded_per_cell(tmp_path):
    """A carried-forward value must be identifiable in the payload, not
    just internally — Overview's "N carried forward" banner and the
    editor's chip both read this off the snapshot, not a client-side
    guess."""
    db = tmp_path / "t.db"
    init_db(db)
    h = store.upsert_holding(db, name="Marcus", kind="cash")
    r1 = store.create_round(db, label="Feb 2026", as_of_date="2026-02-15")
    store.upsert_values(db, round_id=r1, values=[
        {"holding_id": h, "value_cents": 1000},
    ])
    r2 = store.create_round(
        db, label="Jul 2026", as_of_date="2026-07-25", seed_from_previous=True,
    )
    snap = store.build_snapshot(db)
    values = snap["holdings"][0]["values"]
    assert values[0]["is_seeded"] is False   # the original, confirmed entry
    assert values[1]["is_seeded"] is True    # copied forward, unconfirmed

    # Confirming it (even with the same number) clears the flag.
    store.upsert_values(db, round_id=r2, values=[
        {"holding_id": h, "value_cents": 1000},
    ])
    snap2 = store.build_snapshot(db)
    assert snap2["holdings"][0]["values"][1]["is_seeded"] is False


def test_snapshot_emits_full_length_arrays_for_gap_holdings(tmp_path):
    """A holding with no value in an early round still gets a cell.

    The xlsx parser dropped empty cells, which compressed the arrays and
    made InvestmentsOverview.buildSeries() depend on finding a complete
    row. From the DB every array is round-aligned.
    """
    db = tmp_path / "t.db"
    init_db(db)
    old = store.upsert_holding(db, name="Old", kind="cash")
    new = store.upsert_holding(db, name="New", kind="cash")
    _round_with(db, "Feb 2026", "2026-02-15", [{"holding_id": old, "value_cents": 100}])
    _round_with(db, "Jul 2026", "2026-07-25", [
        {"holding_id": old, "value_cents": 150},
        {"holding_id": new, "value_cents": 900},
    ])
    snap = store.build_snapshot(db)
    by_name = {h["name"]: h for h in snap["holdings"]}
    assert len(by_name["New"]["values"]) == 2
    assert by_name["New"]["values"][0]["cents"] == 0
    assert len(by_name["Old"]["values"]) == 2


def test_build_snapshot_round_id_truncates_history(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    h = store.upsert_holding(db, name="Fund", kind="brokerage")
    r1 = _round_with(db, "Feb 2026", "2026-02-15", [
        {"holding_id": h, "value_cents": 100},
    ])
    _round_with(db, "Jul 2026", "2026-07-25", [{"holding_id": h, "value_cents": 200}])
    snap = store.build_snapshot(db, round_id=r1)
    assert snap["as_of"] == "2026-02-15"
    assert [v["cents"] for v in snap["holdings"][0]["values"]] == [100]
