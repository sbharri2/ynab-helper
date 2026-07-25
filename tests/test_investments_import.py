from datetime import date

import pytest
from openpyxl import Workbook

from bot.storage import init_db, connect
from bot import investments_import as imp
from bot import investments_store as store

# NOTE: sqlite3 runs with detect_types=PARSE_DECLTYPES and storage.py:18-21
# registers DATE converters, so DATE columns come back as datetime.date,
# NOT str. Assertions against raw rows must use date(...) objects; only
# build_snapshot's payload is normalized to ISO strings via _as_iso().


def _make_xlsx(path, *, header_dates=("02-15-26", "07-25-26")):
    wb = Workbook()
    ws = wb.active
    ws.append(["Account", "Type", "Number", "Owner",
               f"2026 Value ({header_dates[0]})",
               f"2026 Value ({header_dates[1]})", "Notes"])
    ws.append(["Marcus", "Savings", "1234", "Joint", "$32,571.29", "$25,668.00", ""])
    ws.append(["117 Mayfield Dr", "Home Equity", "", "Joint",
               "$246,501.00", "$249,522.92", "Zestimate"])
    ws.append([])
    ws.append(["Type of Insurance", "Through Employer", "Provider", "Contact",
               "Coverage", "Deductible", "Annual Premium", "Comments", "Renewal"])
    ws.append(["Homeowners", "No", "Amica", "agent", "482k", "$1,000",
               "$2,798.00", "escrow", "2027-01-01"])
    wb.save(path)
    return path


def test_import_creates_rounds_holdings_values(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    xlsx = _make_xlsx(tmp_path / "snap.xlsx")
    result = imp.import_xlsx(db, xlsx)
    assert result["rounds"] == 2
    assert result["holdings"] == 2
    assert result["values"] == 4
    rounds = store.list_rounds(db)
    assert [r["as_of_date"] for r in rounds] == [date(2026, 7, 25), date(2026, 2, 15)]


def test_import_is_idempotent(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    xlsx = _make_xlsx(tmp_path / "snap.xlsx")
    imp.import_xlsx(db, xlsx)
    second = imp.import_xlsx(db, xlsx)
    assert second["rounds"] == 0
    assert len(second["skipped_rounds"]) == 2
    assert len(store.list_rounds(db)) == 2
    assert len(store.list_holdings(db)) == 2
    with connect(db) as con:
        n = con.execute("SELECT COUNT(*) FROM holding_value").fetchone()[0]
    assert n == 4


def test_property_rows_get_property_kind_and_detail(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    imp.import_xlsx(db, _make_xlsx(tmp_path / "snap.xlsx"))
    holdings = {h["name"]: h for h in store.list_holdings(db)}
    assert holdings["117 Mayfield Dr"]["kind"] == "property"
    assert holdings["117 Mayfield Dr"]["is_primary_residence"] == 1
    assert holdings["Marcus"]["kind"] != "property"


def test_import_creates_policy_and_baseline_observation(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    imp.import_xlsx(db, _make_xlsx(tmp_path / "snap.xlsx"))
    with connect(db) as con:
        policies = [dict(r) for r in con.execute("SELECT * FROM insurance_policy")]
        obs = [dict(r) for r in con.execute(
            "SELECT * FROM insurance_premium_observed"
        )]
    assert len(policies) == 1
    assert policies[0]["premium_cents"] == 279800
    assert len(obs) == 1
    assert obs[0]["amount_cents"] == 279800
    assert obs[0]["as_of_date"] == date(2026, 7, 25)   # newest round's date


def test_unparseable_header_raises(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    xlsx = tmp_path / "bad.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["Account", "Type", "Number", "Owner", "Value", "Notes"])
    ws.append(["Marcus", "Savings", "1234", "Joint", "$1.00", ""])
    wb.save(xlsx)
    with pytest.raises(ValueError, match="no parseable date"):
        imp.import_xlsx(db, xlsx)
