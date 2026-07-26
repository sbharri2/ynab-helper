"""Move grooming, exercise and hobby envelopes into the groups they belong to.

Steven, 2026-07-25. The two haircut categories were filed under Monthly
Bills next to the mortgage; they're discretionary per-person spending, so
they move to Personal Spending where the per-person subtotal picks them
up. Exercise moves out of Day to Day Expenses into Hobbies. Golf and
Sewing move the other way, out of Hobbies into Personal Spending, which
leaves Hobbies holding Exercise alone.

Golf and Sewing are also renamed to "Steven Golf" and "Allison Sewing".
Owner is inferred from the category name (see ownerOf in Budget.tsx and
commands.rs:1149) — a bare "Golf" would fall into the Joint bucket rather
than Steven's. Every other envelope in Personal Spending already carries
its owner's name, so this is the house convention, not a new one.

Both haircuts flip to is_spending=1 so the categorizer can suggest them
for a salon charge — they were 0 only because they used to be bills.

Safe to re-run: each entry is keyed by id and accepts either the old or
the new name as its current state, so a second run is a no-op. Moving or
renaming a category does NOT touch month_category (keyed by category_id),
so no budget history changes and nothing needs recomputing.

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

# (category_id, name before, name after, target group_id, is_spending)
# name after == name before means no rename.
MOVES = [
    ("aadf1ab0-5cdf-478a-b471-b09655c9c093",
     "Steven Haircut", "Steven Haircut", PERSONAL_SPENDING, 1),
    ("f2123901-c9c4-431e-94b2-b94f825394a8",
     "Allison Haircut and Perm and Color",
     "Allison Haircut and Perm and Color", PERSONAL_SPENDING, 1),
    ("0c3aa81c-6ab3-48a1-a63c-bdd7164079cd",
     "Exercise", "Exercise", HOBBIES, 1),
    ("local-bdba3435-752e-4f0e-94ed-d115a16f4d59",
     "Golf", "Steven Golf", PERSONAL_SPENDING, 1),
    ("local-682b42c6-9928-4ebf-8a2e-28c4d47775c4",
     "Sewing", "Allison Sewing", PERSONAL_SPENDING, 1),
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
        for cat_id, old_name, new_name, group_id, is_spending in MOVES:
            row = con.execute(
                "SELECT c.name, c.group_id, c.is_spending, g.name AS group_name "
                "FROM category c JOIN category_group g ON g.id = c.group_id "
                "WHERE c.id = ?", (cat_id,),
            ).fetchone()
            if row is None:
                print(f"ABORT: category {cat_id} ({old_name}) not found")
                return 1
            if row["name"] not in (old_name, new_name):
                print(f"ABORT: {cat_id} is named {row['name']!r}, expected "
                      f"{old_name!r} or {new_name!r}")
                return 1
            # A rename must not collide with a different live category.
            if row["name"] != new_name:
                clash = con.execute(
                    "SELECT id FROM category WHERE hidden = 0 "
                    "AND lower(name) = lower(?) AND id != ?",
                    (new_name, cat_id),
                ).fetchone()
                if clash:
                    print(f"ABORT: a category named {new_name!r} already "
                          f"exists ({clash['id']})")
                    return 1

            if (row["group_id"] == group_id
                    and row["is_spending"] == is_spending
                    and row["name"] == new_name):
                print(f"  = {new_name}: already correct, skipping")
                continue

            con.execute(
                "UPDATE category SET name = ?, group_id = ?, is_spending = ? "
                "WHERE id = ?",
                (new_name, group_id, is_spending, cat_id),
            )
            target = con.execute(
                "SELECT name FROM category_group WHERE id = ?", (group_id,),
            ).fetchone()["name"]
            label = (f"{row['name']} -> {new_name}"
                     if row["name"] != new_name else new_name)
            print(f"  -> {label}: {row['group_name']} -> {target}"
                  f"  (is_spending {row['is_spending']} -> {is_spending})")
            audits.append({
                "category_id": cat_id,
                "name_from": row["name"], "name_to": new_name,
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
    print(f"\n{n} categor{'y' if n == 1 else 'ies'} changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
