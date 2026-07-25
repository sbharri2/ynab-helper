"""Exercise the cutover gate script against fixture data.

``scripts/verify_investments_import.py`` is meant to be run by hand against
the real xlsx + live DB, which we must never do from a test. Instead we
build a tiny xlsx fixture whose holdings/totals/insurance sections are
internally consistent (Total == sum of holdings, Minus Home Equity == Total
minus the primary-residence holding), import it into a tmp_path DB with
``investments_import.import_xlsx`` (the same importer the real cutover
uses), and confirm the script agrees the two sides match. Then we mutate
the DB directly and confirm the script catches it and exits 1.

``scripts`` has no ``__init__.py``, so the module is loaded by path with
``importlib`` rather than a normal import.

Fixture has three holdings so the "wrong real-estate row subtracted"
failure mode is representable at all:
  - Marcus            cash, both rounds
  - 117 Mayfield Dr    primary residence, both rounds
  - 456 Rental Ave     second real-estate holding, round 2 only (round 1
                        cell left blank in the xlsx) -- this also gives us
                        a (holding, round) pair the xlsx has no opinion on,
                        which Finding 1's union-of-dates test needs.

Round 1 (02-15-26): Marcus $32,571.29, Mayfield $246,501.00, Rental --
    Total             = $279,072.29
    Minus Home Equity = $32,571.29   (Total minus Mayfield; Rental absent)
Round 2 (07-25-26): Marcus $25,668.00, Mayfield $249,522.92, Rental $121,000.00
    Total             = $396,190.92
    Minus Home Equity = $146,668.00  (Total minus Mayfield = Marcus + Rental)
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from openpyxl import Workbook

from bot.storage import connect, init_db
from bot import investments_import as imp
from bot import investments_store as store

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "verify_investments_import.py"


def _load_verifier():
    spec = importlib.util.spec_from_file_location(
        "verify_investments_import", SCRIPT_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verifier = _load_verifier()


def _make_xlsx(path: Path) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.append([
        "Account", "Type", "Number", "Owner",
        "2026 Value (02-15-26)", "2026 Value (07-25-26)", "Notes",
    ])
    ws.append(["Marcus", "Savings", "1234", "Joint",
               "$32,571.29", "$25,668.00", ""])
    ws.append(["117 Mayfield Dr", "Home Equity", "", "Joint",
               "$246,501.00", "$249,522.92", "Zestimate"])
    ws.append(["456 Rental Ave", "Home Equity", "", "Joint",
               "", "$121,000.00", "Zestimate"])
    ws.append([])
    ws.append(["Total", "$279,072.29", "$396,190.92"])
    ws.append(["Minus Home Equity", "$32,571.29", "$146,668.00"])
    ws.append([])
    ws.append([
        "Type of Insurance", "Through Employer", "Provider", "Contact",
        "Coverage", "Deductible", "Annual Premium", "Comments", "Renewal",
    ])
    ws.append(["Homeowners", "No", "Amica", "agent", "482k", "$1,000",
               "$2,798.00", "escrow", "2027-01-01"])
    wb.save(path)
    return path


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    db = tmp_path / "t.db"
    init_db(db)
    xlsx = _make_xlsx(tmp_path / "snap.xlsx")
    result = imp.import_xlsx(db, xlsx)
    # Sanity check on the importer itself, so a broken fixture fails loudly
    # here instead of masquerading as a verifier bug.
    assert result["holdings"] == 3
    assert result["values"] == 5   # Marcus x2, Mayfield x2, Rental x1
    assert result["policies"] == 1
    return xlsx, db


def _run(monkeypatch, xlsx: Path, db: Path) -> int:
    monkeypatch.setattr(
        "sys.argv",
        ["verify_investments_import.py", "--xlsx", str(xlsx), "--db", str(db)],
    )
    return verifier.main()


def test_matching_import_exits_zero(tmp_path, capsys, monkeypatch):
    xlsx, db = _setup(tmp_path)

    rc = _run(monkeypatch, xlsx, db)

    out = capsys.readouterr().out
    assert rc == 0
    assert "OK — 3 holdings, 2 rounds, 1 policies match." == out.strip()


def test_mutated_db_value_exits_one_and_names_the_diff(tmp_path, capsys, monkeypatch):
    xlsx, db = _setup(tmp_path)

    # Corrupt one holding_value cell directly in the DB, bypassing the
    # store API, to simulate an import that silently went wrong.
    with connect(db) as con:
        con.execute(
            "UPDATE holding_value SET value_cents = value_cents + 100 "
            "WHERE holding_id = (SELECT id FROM holding WHERE name = 'Marcus') "
            "AND as_of_date = '2026-07-25'"
        )
        con.commit()

    rc = _run(monkeypatch, xlsx, db)

    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL" in out
    assert "Marcus @ 2026-07-25" in out


def test_missing_db_holding_exits_one(tmp_path, capsys, monkeypatch):
    xlsx, db = _setup(tmp_path)

    with connect(db) as con:
        con.execute(
            "DELETE FROM holding_value WHERE holding_id = "
            "(SELECT id FROM holding WHERE name = 'Marcus')"
        )
        con.execute("DELETE FROM holding WHERE name = 'Marcus'")
        con.commit()

    rc = _run(monkeypatch, xlsx, db)

    out = capsys.readouterr().out
    assert rc == 1
    assert "missing from DB: Marcus" in out


def test_totals_row_wrong_property_subtracted_exits_one(tmp_path, capsys, monkeypatch):
    """Both real-estate holdings get flagged primary -- the ~$63k-class
    error the review called out ("subtracting both properties ... looks
    completely plausible"). "Minus Home Equity" then subtracts a sum that
    matches no single real-estate holding's value, which is exactly what
    the recomputed check is built to catch.

    (A same-magnitude swap of the flag onto ONLY the rental is NOT
    distinguishable from the correct case by this check: subtracting any
    one real-estate holding's own value trivially "matches" that holding.
    Catching that specific misattribution needs a primary-residence
    identity in the snapshot payload, which neither parse_snapshot nor
    build_snapshot expose today -- out of scope for this script.)
    """
    xlsx, db = _setup(tmp_path)

    with connect(db) as con:
        con.execute(
            "UPDATE property_detail SET is_primary_residence = 1 "
            "WHERE holding_id = (SELECT id FROM holding WHERE name = '456 Rental Ave')"
        )
        con.commit()

    rc = _run(monkeypatch, xlsx, db)

    out = capsys.readouterr().out
    assert rc == 1
    assert "Minus Home Equity" in out


def test_planted_db_value_for_a_blank_xlsx_cell_exits_one(tmp_path, capsys, monkeypatch):
    """Finding 1: garbage in a (holding, round) the xlsx never populated
    for that holding must not be invisible to the gate. Rental's round-1
    cell is blank in the fixture xlsx (its history starts round 2); plant
    a nonzero DB value there directly, bypassing the importer, to
    simulate an import (or a manual entry) that silently went wrong.
    """
    xlsx, db = _setup(tmp_path)

    round1 = next(
        r for r in store.list_rounds(db) if r["as_of_date"].isoformat() == "2026-02-15"
    )
    rental = next(h for h in store.list_holdings(db) if h["name"] == "456 Rental Ave")
    store.upsert_values(
        db, round_id=round1["id"], source="manual",
        values=[{
            "holding_id": rental["id"], "value_cents": 500000,
            "as_of_date": "2026-02-15",
        }],
    )

    rc = _run(monkeypatch, xlsx, db)

    out = capsys.readouterr().out
    assert rc == 1
    assert "456 Rental Ave @ 2026-02-15: xlsx 0 != db 500000" in out


def test_changed_premium_exits_one(tmp_path, capsys, monkeypatch):
    """Finding 3: premium drift, the whole reason this system exists, must
    not be invisible behind a bare row-count check."""
    xlsx, db = _setup(tmp_path)

    with connect(db) as con:
        con.execute(
            "UPDATE insurance_policy SET premium_cents = premium_cents + 10000 "
            "WHERE insurance_type = 'Homeowners'"
        )
        con.commit()

    rc = _run(monkeypatch, xlsx, db)

    out = capsys.readouterr().out
    assert rc == 1
    assert "insurance Homeowners: xlsx premium 279800 != db 289800" in out


def test_undated_column_header_exits_one(tmp_path, capsys, monkeypatch):
    """Finding 1: an unparseable date in a column header must fail the
    gate outright rather than silently collapsing into a shared ``None``
    key that hides the whole column from the diff."""
    _, db = _setup(tmp_path)

    bad_xlsx = tmp_path / "bad.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["Account", "Type", "Number", "Owner", "Current Value", "Notes"])
    ws.append(["Marcus", "Savings", "1234", "Joint", "$100.00", ""])
    wb.save(bad_xlsx)

    rc = _run(monkeypatch, bad_xlsx, db)

    out = capsys.readouterr().out
    assert rc == 1
    assert "Marcus: xlsx has a value column with no parseable date" in out
