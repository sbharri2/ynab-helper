"""Diff the xlsx snapshot against the DB snapshot. Cutover gate.

Usage:
    python scripts/verify_investments_import.py [--xlsx PATH] [--db PATH]

Exits 0 when every holding, every value cell, and every totals row match.
Any difference is printed and exits 1.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import investments as parser          # noqa: E402
from bot import investments_store as store     # noqa: E402

DEFAULT_DB = Path("C:/Users/Steven/ynabhelper/ynab_helper.db")


def _cells_by_date(holding: dict) -> dict[str, int]:
    return {v["snapshot_date"]: v["cents"] for v in holding["values"]}


def _undated(holding: dict) -> bool:
    """True if any column header failed to yield a date.

    Two undated columns collapse into one dict key, silently hiding a whole
    column from the diff. The gate must refuse to certify that.
    """
    return any(not v.get("snapshot_date") for v in holding["values"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx", type=Path, default=None)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = ap.parse_args()

    xlsx = args.xlsx or parser.find_latest_snapshot()
    if xlsx is None:
        print(f"FAIL: no xlsx found in {parser.SNAPSHOTS_DIR}")
        return 1

    sheet = parser.parse_snapshot(xlsx)
    db = store.build_snapshot(args.db)
    problems: list[str] = []

    sheet_h = {h["name"]: h for h in sheet["holdings"]}
    db_h = {h["name"]: h for h in db["holdings"]}

    for name in sorted(set(sheet_h) - set(db_h)):
        problems.append(f"missing from DB: {name}")
    for name in sorted(set(db_h) - set(sheet_h)):
        problems.append(f"extra in DB (not in xlsx): {name}")

    undated_names = {name for name, h in sheet_h.items() if _undated(h)}
    for name in sorted(undated_names):
        problems.append(f"{name}: xlsx has a value column with no parseable date")

    # A holding with an undated column has an untrustworthy `None` key in
    # its date dict, which can't be compared against real ISO date strings
    # (sorted() would raise). It's already been flagged above, so skip its
    # per-date diff rather than let that surface as an unhandled crash.
    for name in sorted((set(sheet_h) & set(db_h)) - undated_names):
        want = _cells_by_date(sheet_h[name])
        got = _cells_by_date(db_h[name])
        for iso in sorted(set(want) | set(got)):
            if want.get(iso, 0) != got.get(iso, 0):
                problems.append(
                    f"{name} @ {iso}: xlsx {want.get(iso, 0)} != db {got.get(iso, 0)}"
                )

    db_dates = (
        [v["snapshot_date"] for v in db["holdings"][0]["values"]]
        if db["holdings"] else []
    )
    db_t = {t["label"]: t["cells"] for t in db["totals_rows"]}
    total_cells = db_t.get("Total", [])
    minus_home = db_t.get("Minus Home Equity", [])

    if len(total_cells) != len(db_dates):
        problems.append(
            f"DB Total has {len(total_cells)} cells for {len(db_dates)} rounds"
        )
    else:
        for i, iso in enumerate(db_dates):
            expected = sum(_cells_by_date(h).get(iso, 0) for h in sheet["holdings"])
            if expected != total_cells[i]:
                problems.append(
                    f"Total @ {iso}: xlsx sum {expected} != db {total_cells[i]}"
                )

    #    The snapshot payload doesn't carry primary-residence identity, only
    #    raw values — so ask the store directly via `is_primary_residence`.
    #    BUT comparing DB total-arithmetic against DB's-own-flagged-holding
    #    is tautological: compute_totals() (inside build_snapshot, used for
    #    total_cells/minus_home) and store.list_holdings() both read the
    #    SAME live property_detail row from the SAME args.db, so they can
    #    never disagree with each other — verified empirically that a pure
    #    single-flag swap (Mayfield -> Rental, nothing else touched) still
    #    returns OK under a self-referential version of this check. Closing
    #    the gap needs an INDEPENDENT expectation of who the primary
    #    residence should be. Mirror the exact heuristic
    #    bot/investments_import.py uses to set the flag in the first place
    #    (name contains "mayfield") and check the DB's actual flag against
    #    THAT, not against its own arithmetic.
    _PRIMARY_NEEDLE = "mayfield"
    xlsx_primary = sorted(
        h["name"] for h in sheet["holdings"]
        if _PRIMARY_NEEDLE in h["name"].lower()
    )
    db_primary = sorted(
        h["name"] for h in store.list_holdings(args.db)
        if h.get("is_primary_residence")
    )
    if xlsx_primary != db_primary:
        problems.append(
            f"primary residence mismatch: xlsx name match ({_PRIMARY_NEEDLE!r}) "
            f"implies {xlsx_primary}, db has is_primary_residence flagged on {db_primary}"
        )
    elif len(db_primary) == 1 and len(minus_home) == len(total_cells) == len(db_dates):
        # Both sides agree on WHO the primary residence is; sanity-check the
        # DB's own subtraction arithmetic against that holding's own value.
        primary = db_primary[0]
        primary_cells = _cells_by_date(db_h[primary]) if primary in db_h else {}
        for i, iso in enumerate(db_dates):
            subtracted = total_cells[i] - minus_home[i]
            expected = primary_cells.get(iso, 0)
            if subtracted != expected:
                problems.append(
                    f"Minus Home Equity @ {iso}: subtracted {subtracted}, but the "
                    f"primary residence ({primary}) is {expected}"
                )

    sheet_ins = {p["insurance_type"]: p for p in sheet["insurance"]}
    db_ins = {p["insurance_type"]: p for p in db["insurance"]}
    if len(sheet_ins) != len(sheet["insurance"]):
        problems.append(
            "xlsx has duplicate insurance types; premiums can't be matched by type"
        )
    for t in sorted(set(sheet_ins) - set(db_ins)):
        problems.append(f"insurance missing from DB: {t}")
    for t in sorted(set(db_ins) - set(sheet_ins)):
        problems.append(f"insurance extra in DB: {t}")
    for t in sorted(set(sheet_ins) & set(db_ins)):
        w = sheet_ins[t].get("annual_premium_cents")
        g = db_ins[t].get("annual_premium_cents")
        if w != g:
            problems.append(f"insurance {t}: xlsx premium {w} != db {g}")
    n_db_ins = len(db["insurance"])

    if problems:
        print(f"FAIL — {len(problems)} difference(s):")
        for p in problems:
            print(f"  - {p}")
        return 1

    print(
        f"OK — {len(db_h)} holdings, "
        f"{len(db_t.get('Total', []))} rounds, {n_db_ins} policies match."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
