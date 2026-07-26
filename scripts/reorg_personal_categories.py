"""Move grooming + exercise envelopes into the groups they belong to.

Steven, 2026-07-25. The two haircut categories were filed under Monthly
Bills next to the mortgage; they're discretionary per-person spending, so
they move to Personal Spending where the per-person subtotal picks them
up (owner is inferred from the category name). Exercise moves out of
Day to Day Expenses into Hobbies.

Both haircuts also flip to is_spending=1 so the categorizer can suggest
them for a salon charge — they were 0 only because they used to be bills.

Safe to re-run: every statement is keyed by id and matches on the current
value, so a second run is a no-op. Moving a category between groups does
NOT touch month_category (keyed by category_id), so no budget history
changes and nothing needs recomputing.

Spec: docs/superpowers/specs/2026-07-25-personal-category-reorg-design.md
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import storage  # noqa: E402

DB_PATH = Path(__file__).resolve().parents[1] / "ynab_helper.db"

PERSONAL_SPENDING = "689417f2-afcf-4a6c-a94a-265818711cac"
HOBBIES = "local-2801bbb7-ba9d-4095-8f9f-ed666f3336d0"

# (category_id, expected name, target group_id, target is_spending)
MOVES = [
    ("aadf1ab0-5cdf-478a-b471-b09655c9c093",
     "Steven Haircut", PERSONAL_SPENDING, 1),
    ("f2123901-c9c4-431e-94b2-b94f825394a8",
     "Allison Haircut and Perm and Color", PERSONAL_SPENDING, 1),
    ("0c3aa81c-6ab3-48a1-a63c-bdd7164079cd",
     "Exercise", HOBBIES, 1),
]


def main() -> int:
    with storage.connect(DB_PATH) as con:
        for gid in (PERSONAL_SPENDING, HOBBIES):
            if not con.execute(
                "SELECT 1 FROM category_group WHERE id = ?", (gid,)
            ).fetchone():
                print(f"ABORT: category group {gid} not found")
                return 1

        audits = []
        for cat_id, expected_name, group_id, is_spending in MOVES:
            row = con.execute(
                "SELECT c.name, c.group_id, c.is_spending, g.name AS group_name "
                "FROM category c JOIN category_group g ON g.id = c.group_id "
                "WHERE c.id = ?", (cat_id,),
            ).fetchone()
            if row is None:
                print(f"ABORT: category {cat_id} ({expected_name}) not found")
                return 1
            if row["name"] != expected_name:
                print(f"ABORT: {cat_id} is named {row['name']!r}, "
                      f"expected {expected_name!r}")
                return 1
            if row["group_id"] == group_id and row["is_spending"] == is_spending:
                print(f"  = {expected_name}: already correct, skipping")
                continue

            con.execute(
                "UPDATE category SET group_id = ?, is_spending = ? WHERE id = ?",
                (group_id, is_spending, cat_id),
            )
            target = con.execute(
                "SELECT name FROM category_group WHERE id = ?", (group_id,),
            ).fetchone()["name"]
            print(f"  -> {expected_name}: {row['group_name']} -> {target}"
                  f"  (is_spending {row['is_spending']} -> {is_spending})")
            audits.append({
                "category_id": cat_id, "name": expected_name,
                "from_group": row["group_name"], "to_group": target,
                "is_spending_from": row["is_spending"],
                "is_spending_to": is_spending,
                "reason": "2026-07-25 personal category reorg",
            })

    # audit() opens its own connection — do it after the write transaction
    # above has committed, or it blocks on the write lock.
    for entry in audits:
        storage.audit(DB_PATH, "category_regroup", entry)

    n = len(audits)
    print(f"\n{n} categor{'y' if n == 1 else 'ies'} moved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
