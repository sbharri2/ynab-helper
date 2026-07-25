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

    for name in sorted(set(sheet_h) & set(db_h)):
        want = _cells_by_date(sheet_h[name])
        got = _cells_by_date(db_h[name])
        for iso, cents in sorted(want.items()):
            if got.get(iso) != cents:
                problems.append(
                    f"{name} @ {iso}: xlsx {cents} != db {got.get(iso)}"
                )

    sheet_t = {t["label"]: t["cells"] for t in sheet["totals_rows"]}
    db_t = {t["label"]: t["cells"] for t in db["totals_rows"]}
    for label in ("Total", "Minus Home Equity"):
        want, got = sheet_t.get(label), db_t.get(label)
        if want is None:
            problems.append(f"xlsx has no {label!r} row")
            continue
        want_clean = [c for c in want if c is not None]
        if want_clean != got:
            problems.append(f"{label}: xlsx {want_clean} != db {got}")

    n_sheet_ins = len(sheet["insurance"])
    n_db_ins = len(db["insurance"])
    if n_sheet_ins != n_db_ins:
        problems.append(f"insurance rows: xlsx {n_sheet_ins} != db {n_db_ins}")

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
