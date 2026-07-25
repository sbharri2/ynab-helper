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

Holding identity is the ``(name, account_type, owner)`` triple, not name
alone -- the real cutover has 32 holding rows but only 29 distinct names
(``_hkey`` in the script). Diff messages format that triple as
``"name (account_type, owner)"`` (``_readable``), so every assertion below
that names a holding includes it.

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
    assert "Marcus (Savings, Joint) @ 2026-07-25" in out


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
    assert "missing from DB: Marcus (Savings, Joint)" in out


def test_totals_row_both_properties_flagged_exits_one(tmp_path, capsys, monkeypatch):
    """Both real-estate holdings get flagged primary -- the ~$63k-class
    error the review called out ("subtracting both properties ... looks
    completely plausible"). The db's flagged-primary set (both properties)
    no longer matches the xlsx's name-based expectation (only Mayfield),
    so the identity cross-check catches it directly.
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
    assert "primary residence mismatch" in out


def test_totals_row_wrong_single_property_flagged_exits_one(tmp_path, capsys, monkeypatch):
    """THE test that would have caught the February misreading: the flag
    moves from the primary residence onto the OTHER real-estate holding --
    one flag total, not two, and the two holdings have different values
    ($249,522.92 vs $121,000.00 in round 2).

    This exact mutation was proven to pass silently (rc == 0, false OK)
    under two prior implementations, both empirically verified against
    this actual fixture before being ruled out -- not assumed:

    1. The original `re_values` heuristic (round 2 review): subtracted
       amount ($121,000.00, the rental's own value) is trivially a member
       of "some real-estate holding's value" once the rental is real
       estate too, so `subtracted not in re_values` was False. rc == 0.

    2. A self-referential `primary_names`-only version (this round's
       initial fix, matching the reviewer's first-pass snippet literally):
       `total_cells`/`minus_home` (from `build_snapshot(args.db)` ->
       `compute_totals`) and `primary_names` (from
       `store.list_holdings(args.db)`) both read the SAME live
       `property_detail` row from the SAME db at call time, so a swapped
       flag can never disagree with itself -- it only catches a count of
       flagged rows != 1, not a wrong SINGLE row. rc == 0.

    The version actually shipped adds an independent expectation: it
    mirrors bot/investments_import.py's own heuristic for setting the flag
    in the first place (holding name contains "mayfield") and checks the
    db's actual flag against THAT, not against the db's own arithmetic.
    """
    xlsx, db = _setup(tmp_path)

    with connect(db) as con:
        con.execute(
            "UPDATE property_detail SET is_primary_residence = 0 "
            "WHERE holding_id = (SELECT id FROM holding WHERE name = '117 Mayfield Dr')"
        )
        con.execute(
            "UPDATE property_detail SET is_primary_residence = 1 "
            "WHERE holding_id = (SELECT id FROM holding WHERE name = '456 Rental Ave')"
        )
        con.commit()

    rc = _run(monkeypatch, xlsx, db)

    out = capsys.readouterr().out
    assert rc == 1
    assert (
        "primary residence mismatch: xlsx name match ('mayfield') implies "
        "['117 Mayfield Dr'], db has is_primary_residence flagged on "
        "['456 Rental Ave']"
    ) in out


def test_totals_row_zero_properties_flagged_exits_one(tmp_path, capsys, monkeypatch):
    """No holding flagged primary at all: Minus Home Equity silently
    equals Total. The old `re_values` check explicitly `continue`d past a
    zero `subtracted` amount, treating "nothing was subtracted" as
    automatically fine -- it is not; the xlsx's own name-based expectation
    says exactly one holding (Mayfield) should be flagged.
    """
    xlsx, db = _setup(tmp_path)

    with connect(db) as con:
        con.execute(
            "UPDATE property_detail SET is_primary_residence = 0 "
            "WHERE holding_id = (SELECT id FROM holding WHERE name = '117 Mayfield Dr')"
        )
        con.commit()

    rc = _run(monkeypatch, xlsx, db)

    out = capsys.readouterr().out
    assert rc == 1
    assert (
        "primary residence mismatch: xlsx name match ('mayfield') implies "
        "['117 Mayfield Dr'], db has is_primary_residence flagged on []"
    ) in out


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
    assert "456 Rental Ave (Home Equity, Joint) @ 2026-02-15: xlsx 0 != db 500000" in out


def test_changed_premium_exits_one(tmp_path, capsys, monkeypatch):
    """Finding 3: premium drift, the whole reason this system exists, must
    not be invisible behind a bare row-count check.

    Insurance has no identity to key on (see the fixture in
    ``test_duplicate_insurance_type_changed_premium_exits_one`` for the
    case where two rows genuinely share type/provider/coverage) -- here
    there's only one "Homeowners" row, so mutating its premium in the DB
    makes the whole 4-tuple a member of one multiset and not the other.
    """
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
    assert "insurance in xlsx but not DB (x1): ('Homeowners', 'Amica', '482k', 279800)" in out
    assert "insurance in DB but not xlsx (x1): ('Homeowners', 'Amica', '482k', 289800)" in out


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
    assert (
        "Marcus (Savings, Joint): xlsx has a value column with no parseable date"
    ) in out


def _make_xlsx_duplicate_name_holdings(path: Path) -> Path:
    """Mirrors the real cutover bug: same holding NAME, different
    account_type -- "Schwab (Transfered from TD AmeriTrade)" is both a
    Roth IRA and a Stock Account with very different balances. Name-only
    keying collapsed the two into one dict entry and lost $25,606.62.
    """
    wb = Workbook()
    ws = wb.active
    ws.append([
        "Account", "Type", "Number", "Owner",
        "2026 Value (02-15-26)", "2026 Value (07-25-26)", "Notes",
    ])
    ws.append(["Schwab (Transfered from TD AmeriTrade)", "Roth IRA", "1111",
               "Steven", "$300.00", "$393.60", ""])
    ws.append(["Schwab (Transfered from TD AmeriTrade)", "Stock Account", "2222",
               "Steven", "$20,000.00", "$25,606.62", ""])
    wb.save(path)
    return path


def _setup_duplicate_name_holdings(tmp_path: Path) -> tuple[Path, Path]:
    db = tmp_path / "dup.db"
    init_db(db)
    xlsx = _make_xlsx_duplicate_name_holdings(tmp_path / "dup.xlsx")
    result = imp.import_xlsx(db, xlsx)
    assert result["holdings"] == 2
    assert result["values"] == 4
    return xlsx, db


def test_duplicate_name_holdings_both_present_exits_zero(tmp_path, capsys, monkeypatch):
    """Both holdings correctly imported and untouched -- the gate should
    say so accurately. This is also where the identity-blind bug is most
    directly demonstrable, not the drop scenario (see the docstring on
    test_duplicate_name_holding_dropped_from_db_exits_one): run this exact
    fixture against commit a404f64 (name-keyed `sheet_h`) with NOTHING
    corrupted, and it prints "OK — 1 holdings, 2 rounds, 0 policies
    match." -- a clean rc == 0, but the count is a lie. There are 2
    holdings, both correctly present with correct values; the name-keyed
    dict discarded one of them before the count was ever taken, with no
    monetary discrepancy for the Total-sum check to catch since nothing is
    actually wrong. The current script correctly reports 2.
    """
    xlsx, db = _setup_duplicate_name_holdings(tmp_path)

    rc = _run(monkeypatch, xlsx, db)

    out = capsys.readouterr().out
    assert rc == 0
    assert "OK — 2 holdings, 2 rounds, 0 policies match." == out.strip()


def test_duplicate_name_holding_dropped_from_db_exits_one(tmp_path, capsys, monkeypatch):
    """Fix 1: identity is (name, account_type, owner), not name alone.

    Drops the Stock Account twin from the DB while the Roth IRA twin (same
    name) survives -- the exact shape of bug the gate caught on the real
    cutover ($25,606.62 lost).

    NOTE on what this test actually proves, checked empirically both ways
    (see task-7-report.md for the full transcript) rather than assumed:
    dropping either twin still returns rc == 1 under the prior name-keyed
    `sheet_h = {h["name"]: h ...}` version (commit a404f64) too -- but via
    a *different*, less precise route. That version's `sheet_h`/`db_h`
    dicts do collapse to one entry per name (silently discarding whichever
    duplicate isn't last), but the Total-row recompute added in round 2
    sums over the RAW `sheet["holdings"]` list, not the name-keyed dict, so
    it independently notices the aggregate dollar amount is short and
    fails with a vague `Total @ <date>: xlsx sum ... != db ...` that does
    not say which holding is missing.

    What this fix concretely buys, provable end to end:
      1. A precise, actionable diagnostic -- "missing from DB: Schwab
         (Transfered from TD AmeriTrade) (Stock Account, Steven)" instead
         of an aggregate number mismatch you'd have to manually trace back
         to a holding.
      2. The missing/extra SET-equality checks themselves stop being
         identity-blind, which matters independent of whether the Total
         safety net happens to also fire -- e.g. it has no equivalent
         backstop for insurance at all (Fix 2), and a future change to how
         Total is computed could silently remove today's redundancy.

    I could not construct a fixture where the OLD script returns a true
    rc == 0 for a dropped/corrupted duplicate-named HOLDING specifically:
    any nonzero value discrepancy changes the DB's aggregate total, and
    swapping values between the two twins instead of dropping one still
    gets caught, because the "last" name-collision survivor a plain dict
    keeps is exactly the entry the old per-holding check compares -- so
    corrupting it is caught there, and corrupting the discarded twin
    instead changes Total. The safety net that makes this hard to
    reproduce for holdings does not exist for insurance at all, which is
    why Fix 2's multiset rewrite is the one with a directly demonstrable
    false-OK closure (see test_duplicate_insurance_type_changed_premium_exits_one).
    """
    xlsx, db = _setup_duplicate_name_holdings(tmp_path)

    with connect(db) as con:
        con.execute(
            "DELETE FROM holding_value WHERE holding_id = (SELECT id FROM holding "
            "WHERE name = 'Schwab (Transfered from TD AmeriTrade)' "
            "AND account_type = 'Stock Account')"
        )
        con.execute(
            "DELETE FROM holding WHERE name = 'Schwab (Transfered from TD AmeriTrade)' "
            "AND account_type = 'Stock Account'"
        )
        con.commit()

    rc = _run(monkeypatch, xlsx, db)

    out = capsys.readouterr().out
    assert rc == 1
    assert (
        "missing from DB: Schwab (Transfered from TD AmeriTrade) "
        "(Stock Account, Steven)"
    ) in out


def _make_xlsx_duplicate_type_insurance(path: Path) -> Path:
    """Mirrors the real cutover data: "Life - Steven" appears four times
    across providers, and two of those share provider AND coverage,
    differing only in premium. A single holding with a parseable date
    column is required too -- import_xlsx raises if it can't establish at
    least one round.
    """
    wb = Workbook()
    ws = wb.active
    ws.append([
        "Account", "Type", "Number", "Owner",
        "2026 Value (02-15-26)", "Notes",
    ])
    ws.append(["Marcus", "Savings", "1234", "Joint", "$100.00", ""])
    ws.append([])
    ws.append([
        "Type of Insurance", "Through Employer", "Provider", "Contact",
        "Coverage", "Deductible", "Annual Premium", "Comments", "Renewal",
    ])
    ws.append(["Life - Steven", "No", "MetLife", "agent1", "$500k", "$0",
               "$214.08", "", ""])
    ws.append(["Life - Steven", "No", "MetLife", "agent1", "$500k", "$0",
               "$151.44", "", ""])
    wb.save(path)
    return path


def _setup_duplicate_type_insurance(tmp_path: Path) -> tuple[Path, Path]:
    db = tmp_path / "ins.db"
    init_db(db)
    xlsx = _make_xlsx_duplicate_type_insurance(tmp_path / "ins.xlsx")
    result = imp.import_xlsx(db, xlsx)
    # import_xlsx's dedupe-by-type set is snapshotted once before the loop
    # and never updated inside it, so both same-type rows land as separate
    # policies on a single import run -- matching the real 28-rows/22-types
    # shape rather than silently dropping one.
    assert result["policies"] == 2
    return xlsx, db


def test_duplicate_insurance_type_clean_import_exits_zero(tmp_path, capsys, monkeypatch):
    """Fix 2's most directly demonstrable win, checked empirically (see
    task-7-report.md) rather than assumed: this exact fixture, fully
    correct and untouched, returns rc == 1 under commit a404f64 (the
    retired type-keyed dict) with `xlsx has duplicate insurance types;
    premiums can't be matched by type` -- an unconditional warning that
    fires on ANY duplicate type regardless of whether the data is right.
    The real xlsx has legitimate duplicate types ("Life - Steven" appears
    four times), so the old gate could never pass cleanly on real data at
    all. The multiset comparison can tell "duplicates present, all
    premiums accounted for" apart from "duplicates present and one is
    wrong" -- this test is the former.
    """
    xlsx, db = _setup_duplicate_type_insurance(tmp_path)

    rc = _run(monkeypatch, xlsx, db)

    out = capsys.readouterr().out
    assert rc == 0, out


def test_duplicate_insurance_type_changed_premium_exits_one(tmp_path, capsys, monkeypatch):
    """Fix 2: insurance has no identity to key on -- compare whole-row
    multisets. Two "Life - Steven" / MetLife / $500k rows differing only
    by premium; corrupt one premium directly in the DB and confirm the
    multiset diff catches it precisely, naming both the missing-in-DB and
    extra-in-DB rows.
    """
    xlsx, db = _setup_duplicate_type_insurance(tmp_path)

    with connect(db) as con:
        con.execute(
            "UPDATE insurance_policy SET premium_cents = 99999 "
            "WHERE insurance_type = 'Life - Steven' AND premium_cents = 15144"
        )
        con.commit()

    rc = _run(monkeypatch, xlsx, db)

    out = capsys.readouterr().out
    assert rc == 1
    assert "insurance in xlsx but not DB (x1): ('Life - Steven', 'MetLife', '$500k', 15144)" in out
    assert "insurance in DB but not xlsx (x1): ('Life - Steven', 'MetLife', '$500k', 99999)" in out
