"""Diff the xlsx snapshot against the DB snapshot. Cutover gate.

Usage:
    python scripts/verify_investments_import.py [--xlsx PATH] [--db PATH]

Exits 0 when every holding, every value cell, and every totals row match.
Any difference is printed and exits 1.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
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


def _hkey(h: dict) -> tuple:
    """A holding's real identity is not its name alone.

    32 real rows import to only 29 distinct names -- e.g. "Schwab
    (Transfered from TD AmeriTrade)" is both a Roth IRA ($393.60) and a
    Stock Account ($25,606.62). Name-only keying let a merged-away
    holding compare equal to its surviving twin and lost $25,606.62 in
    the real cutover. account_type + owner disambiguate.
    """
    return (h["name"], h.get("account_type") or None, h.get("owner") or None)


def _readable(k: tuple) -> str:
    return f"{k[0]} ({k[1]}, {k[2]})"


def _sort_key(k: tuple) -> tuple:
    # Tuple elements can be None (undated columns, missing account_type),
    # and sorting a set of mixed None/str tuples raises TypeError the
    # moment two keys share their first field or more -- exactly the
    # duplicate-name case this function exists to sort. Stringify first.
    return tuple(str(x) for x in k)


def _ikey(p: dict) -> tuple:
    """Insurance has no identity to key on at all. "Life - Steven" appears
    four times across providers, and two of those share provider AND
    coverage, differing only in premium ($214.08 vs $151.44). Compare the
    whole row as a multiset member instead of pretending a key exists.
    """
    return (
        p.get("insurance_type"), p.get("provider") or "",
        p.get("coverage") or "", p.get("annual_premium_cents"),
    )


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

    # Identity is the (name, account_type, owner) triple, not name alone --
    # see _hkey. Report duplicate keys on either side rather than silently
    # collapsing them: if the sheet or DB ever carries two rows identical
    # in all three fields, the dict comprehension below would hide one of
    # them the same way plain name-keying did.
    sheet_key_counts = Counter(_hkey(h) for h in sheet["holdings"])
    db_key_counts = Counter(_hkey(h) for h in db["holdings"])
    for k in sorted((k for k, n in sheet_key_counts.items() if n > 1), key=_sort_key):
        problems.append(
            f"xlsx has {sheet_key_counts[k]} holdings with identical "
            f"(name, account_type, owner) {_readable(k)}"
        )
    for k in sorted((k for k, n in db_key_counts.items() if n > 1), key=_sort_key):
        problems.append(
            f"db has {db_key_counts[k]} holdings with identical "
            f"(name, account_type, owner) {_readable(k)}"
        )

    sheet_h = {_hkey(h): h for h in sheet["holdings"]}
    db_h = {_hkey(h): h for h in db["holdings"]}

    for k in sorted(set(sheet_h) - set(db_h), key=_sort_key):
        problems.append(f"missing from DB: {_readable(k)}")
    for k in sorted(set(db_h) - set(sheet_h), key=_sort_key):
        problems.append(f"extra in DB (not in xlsx): {_readable(k)}")

    undated_keys = {k for k, h in sheet_h.items() if _undated(h)}
    for k in sorted(undated_keys, key=_sort_key):
        problems.append(f"{_readable(k)}: xlsx has a value column with no parseable date")

    # A holding with an undated column has an untrustworthy `None` key in
    # its date dict, which can't be compared against real ISO date strings
    # (sorted() would raise). It's already been flagged above, so skip its
    # per-date diff rather than let that surface as an unhandled crash.
    for k in sorted((set(sheet_h) & set(db_h)) - undated_keys, key=_sort_key):
        want = _cells_by_date(sheet_h[k])
        got = _cells_by_date(db_h[k])
        for iso in sorted(set(want) | set(got)):
            if want.get(iso, 0) != got.get(iso, 0):
                problems.append(
                    f"{_readable(k)} @ {iso}: xlsx {want.get(iso, 0)} != db {got.get(iso, 0)}"
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
        # db_h is keyed by the (name, account_type, owner) triple now, not
        # by name alone -- a name match can hit more than one entry (the
        # same duplicate-name problem holdings have generally), so that is
        # reported rather than silently picking whichever one dict lookup
        # would have returned.
        primary = db_primary[0]
        primary_matches = [k for k in db_h if k[0] == primary]
        if len(primary_matches) != 1:
            problems.append(
                f"primary residence name {primary!r} matches "
                f"{len(primary_matches)} holdings in the DB, not 1: "
                f"{sorted(_readable(k) for k in primary_matches)}"
            )
        else:
            primary_cells = _cells_by_date(db_h[primary_matches[0]])
            for i, iso in enumerate(db_dates):
                subtracted = total_cells[i] - minus_home[i]
                expected = primary_cells.get(iso, 0)
                if subtracted != expected:
                    problems.append(
                        f"Minus Home Equity @ {iso}: subtracted {subtracted}, but the "
                        f"primary residence ({primary}) is {expected}"
                    )

    # Insurance has no identity to key on at all (see _ikey) -- 28 rows,
    # 22 distinct types, and two rows sharing type+provider+coverage that
    # differ only by premium. Compare whole-row multisets instead.
    sheet_ins = Counter(_ikey(p) for p in sheet["insurance"])
    db_ins = Counter(_ikey(p) for p in db["insurance"])
    for tup, n in sorted((sheet_ins - db_ins).items(), key=lambda item: _sort_key(item[0])):
        problems.append(f"insurance in xlsx but not DB (x{n}): {tup}")
    for tup, n in sorted((db_ins - sheet_ins).items(), key=lambda item: _sort_key(item[0])):
        problems.append(f"insurance in DB but not xlsx (x{n}): {tup}")
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
