"""Mark existing YNAB investment-related categories as is_spending=0.

Per Steven's 2026-06-26 directive: money flowing into savings/investment
vehicles is NOT spending. Today many of his transactions are already
categorized into meaningful buckets like 'Kids Stock Investment' or
'Steven Retirement Investment' — those categories just have
is_spending=1, so they pollute spending analytics.

This script flips the flag. We DON'T touch the transactions themselves
(they stay categorized exactly as before), we just tell the analytics
layer 'these aren't spending.'

Dry-run by default. Re-run with --apply to write.
"""
from __future__ import annotations
import argparse
import sys

from bot import storage
from bot.config import load_settings

# Categories whose name + intent maps to "savings/investment flow."
# Conservative list — only categories Steven actually uses.
INVESTMENT_NAME_PATTERNS = [
    "Kids Stock Investment",
    "Steven Retirement Investment",
    "Steven Personal Savings",
    "Allison Personal Savings",
    "Alternate Investment",
    "Business Savings",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    settings = load_settings()
    db_path = settings.paths.database

    with storage.connect(db_path) as con:
        # Find all matching categories that are currently is_spending=1.
        placeholders = ",".join(["?"] * len(INVESTMENT_NAME_PATTERNS))
        rows = con.execute(
            f"SELECT c.id, c.name, c.is_spending, g.name AS group_name "
            f"FROM category c JOIN category_group g ON g.id = c.group_id "
            f"WHERE c.name IN ({placeholders}) AND c.hidden = 0 "
            f"ORDER BY c.name",
            INVESTMENT_NAME_PATTERNS,
        ).fetchall()

    if not rows:
        print("None of the listed categories found in DB.")
        return 1

    print("Found:")
    will_flip = []
    for r in rows:
        marker = "→ will flip" if r["is_spending"] == 1 else "  already non-spending"
        print(f"  {r['name']:35s}  is_spending={r['is_spending']}  ({r['group_name']})  {marker}")
        if r["is_spending"] == 1:
            will_flip.append(r["id"])

    if not will_flip:
        print("\nNothing to do — all already non-spending.")
        return 0

    if not args.apply:
        print(f"\nDRY-RUN — {len(will_flip)} categories to flip. Pass --apply to write.")
        return 0

    print(f"\nApplying — flipping {len(will_flip)} categories…")
    placeholders = ",".join(["?"] * len(will_flip))
    with storage.connect(db_path) as con:
        con.execute(
            f"UPDATE category SET is_spending = 0 WHERE id IN ({placeholders})",
            will_flip,
        )
    storage.audit(db_path, "investment_categories_flipped", {
        "category_ids": will_flip,
        "names": [r["name"] for r in rows if r["id"] in will_flip],
    })
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
