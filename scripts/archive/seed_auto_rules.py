"""One-shot: copy bot/payee_overrides.OVERRIDES into the auto_rule table.

After this runs, the Auto-Sync panel owns these rules and the OVERRIDES
literal can be deleted. A rule whose category no longer exists in the DB is
REPORTED, not silently dropped -- a missing envelope is a data problem worth
seeing, and swallowing it would quietly stop a bill from auto-filing.

Usage:
    ./.venv/Scripts/python.exe scripts/seed_auto_rules.py --dry-run
    ./.venv/Scripts/python.exe scripts/seed_auto_rules.py
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import storage                      # noqa: E402
from bot.payee_overrides import OVERRIDES    # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="ynab_helper.db")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    db = Path(args.db)

    existing = {r["pattern"] for r in storage.list_auto_rules(db)}
    created = skipped_dupe = 0
    missing: list[tuple[str, str]] = []

    for rule in OVERRIDES:
        pattern = rule.pattern.pattern
        if pattern in existing:
            skipped_dupe += 1
            continue
        with storage.connect(db) as con:
            row = con.execute(
                "SELECT id FROM category WHERE LOWER(name) = LOWER(?) "
                "AND hidden = 0", (rule.category_name,),
            ).fetchone()
        if row is None:
            missing.append((pattern, rule.category_name))
            continue
        print(f"  {pattern[:46]:46} -> {rule.category_name}")
        if not args.dry_run:
            try:
                storage.create_auto_rule(
                    db, pattern=pattern, category_id=row["id"],
                    created_by="seed",
                    note="migrated from payee_overrides.OVERRIDES",
                )
            except sqlite3.IntegrityError:
                skipped_dupe += 1
                continue
        created += 1

    print(f"\ncreated={created} already_present={skipped_dupe} "
          f"missing_category={len(missing)}")
    for pattern, name in missing:
        print(f"  MISSING CATEGORY {name!r} for pattern {pattern!r}")
    if args.dry_run:
        print("(dry-run: nothing written)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
