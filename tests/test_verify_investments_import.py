"""Exercise the cutover gate script against fixture data.

``scripts/verify_investments_import.py`` is meant to be run by hand against
the real xlsx + live DB, which we must never do from a test. Instead we
build a tiny xlsx fixture whose holdings/totals/insurance sections are
internally consistent (Total == sum of holdings, Minus Home Equity == Total
minus the primary-residence holding), import it into a tmp_path DB with
``investments_import.import_xlsx`` (the same importer the real cutover
uses), and confirm the script agrees the two sides match. Then we mutate
one DB value directly and confirm the script catches it and exits 1.

``scripts`` has no ``__init__.py``, so the module is loaded by path with
``importlib`` rather than a normal import.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from openpyxl import Workbook

from bot.storage import connect, init_db
from bot import investments_import as imp

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
    """Two holdings (one the primary residence) + a consistent totals
    block + one insurance row.

    Round 1 (02-15-26): Marcus $32,571.29, Mayfield $246,501.00
        Total            = $279,072.29
        Minus Home Equity = $32,571.29  (Total minus Mayfield)
    Round 2 (07-25-26): Marcus $25,668.00, Mayfield $249,522.92
        Total            = $275,190.92
        Minus Home Equity = $25,668.00
    """
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
    ws.append([])
    ws.append(["Total", "$279,072.29", "$275,190.92"])
    ws.append(["Minus Home Equity", "$32,571.29", "$25,668.00"])
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
    assert result["holdings"] == 2
    assert result["values"] == 4
    assert result["policies"] == 1
    return xlsx, db


def test_matching_import_exits_zero(tmp_path, capsys):
    xlsx, db = _setup(tmp_path)

    sys.argv = [
        "verify_investments_import.py",
        "--xlsx", str(xlsx),
        "--db", str(db),
    ]
    rc = verifier.main()

    out = capsys.readouterr().out
    assert rc == 0
    assert "OK — 2 holdings, 2 rounds, 1 policies match." == out.strip()


def test_mutated_db_value_exits_one_and_names_the_diff(tmp_path, capsys):
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

    sys.argv = [
        "verify_investments_import.py",
        "--xlsx", str(xlsx),
        "--db", str(db),
    ]
    rc = verifier.main()

    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL" in out
    assert "Marcus @ 2026-07-25" in out


def test_missing_db_holding_exits_one(tmp_path, capsys):
    xlsx, db = _setup(tmp_path)

    with connect(db) as con:
        con.execute(
            "DELETE FROM holding_value WHERE holding_id = "
            "(SELECT id FROM holding WHERE name = 'Marcus')"
        )
        con.execute("DELETE FROM holding WHERE name = 'Marcus'")
        con.commit()

    sys.argv = [
        "verify_investments_import.py",
        "--xlsx", str(xlsx),
        "--db", str(db),
    ]
    rc = verifier.main()

    out = capsys.readouterr().out
    assert rc == 1
    assert "missing from DB: Marcus" in out


def test_totals_row_mismatch_exits_one(tmp_path, capsys):
    xlsx, db = _setup(tmp_path)

    # Break the totals gate directly: flip which holding is flagged as the
    # primary residence, so compute_totals subtracts the wrong row from
    # "Minus Home Equity" — this is the exact failure mode Task 7's brief
    # calls out (property_detail.is_primary_residence on the wrong row).
    with connect(db) as con:
        con.execute("UPDATE property_detail SET is_primary_residence = 0")
        con.commit()

    sys.argv = [
        "verify_investments_import.py",
        "--xlsx", str(xlsx),
        "--db", str(db),
    ]
    rc = verifier.main()

    out = capsys.readouterr().out
    assert rc == 1
    assert "Minus Home Equity" in out
