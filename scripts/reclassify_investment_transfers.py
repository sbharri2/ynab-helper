"""One-shot: scan ledger_txn for transactions matching investment-payee
patterns and apply the override-matched category to any row that's
currently uncategorized OR categorized into a spending category.

Default: --dry-run (lists what WOULD change). Re-run with --apply to
write.

Safety:
  * Skips rows whose category is already in the Investments/Savings
    Transfers group (idempotent re-runs are a no-op).
  * Skips rows with payee LIKE 'Transfer :%' — those are YNAB internal
    transfers and shouldn't be touched.
"""
from __future__ import annotations
import argparse
import sys
from datetime import datetime, timezone

from bot import storage
from bot.config import load_settings
from bot.payee_overrides import resolve_payee_override

INVESTMENTS_GROUP = "Investments / Savings Transfers"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="actually write changes (default: dry-run preview)")
    parser.add_argument("--limit", type=int, default=None,
                        help="cap number of rows changed")
    args = parser.parse_args()

    settings = load_settings()
    db_path = settings.paths.database

    # Pull the set of investment-category ids so we can skip rows that
    # are already there.
    with storage.connect(db_path) as con:
        inv_cat_ids = {
            r["id"] for r in con.execute(
                "SELECT c.id FROM category c "
                "JOIN category_group g ON g.id = c.group_id "
                "WHERE g.name = ?",
                (INVESTMENTS_GROUP,),
            ).fetchall()
        }
    if not inv_cat_ids:
        print(f"No categories in '{INVESTMENTS_GROUP}' group. "
              f"Run bootstrap_investment_categories.py first.")
        return 1

    # Iterate over every ledger_txn (outflows) — let the override matcher
    # decide which ones apply. This is the simplest correct approach;
    # ~13k rows is well within "fast enough" for a one-shot.
    with storage.connect(db_path) as con:
        rows = con.execute(
            "SELECT id, posted_date, amount_cents, payee, category_id "
            "FROM ledger_txn "
            "WHERE amount_cents < 0 "
            "  AND (payee IS NULL OR payee NOT LIKE 'Transfer :%') "
            "ORDER BY posted_date DESC"
        ).fetchall()

    print(f"scanning {len(rows):,} outflow rows…")

    matched = 0
    skipped_already = 0
    by_dest: dict[str, int] = {}
    plan: list[tuple[int, str, int, str, str, str, str]] = []
    # (id, posted_date, amount, payee, new_cat_name, new_cat_id, current_cat)

    for r in rows:
        ovr = resolve_payee_override(db_path, r["payee"] or "")
        if ovr is None:
            continue
        new_cat = ovr["category_id"]
        if new_cat not in inv_cat_ids:
            # Match was a non-investment override (e.g. AT&T). Skip — this
            # script only handles investment moves.
            continue
        if r["category_id"] == new_cat:
            skipped_already += 1
            continue
        matched += 1
        by_dest[ovr["category_name"]] = by_dest.get(ovr["category_name"], 0) + 1
        # current category name for display
        with storage.connect(db_path) as con:
            cur_row = (con.execute(
                "SELECT name FROM category WHERE id = ?", (r["category_id"],)
            ).fetchone() if r["category_id"] else None)
        cur_name = cur_row["name"] if cur_row else "(uncategorized)"
        plan.append((
            r["id"], str(r["posted_date"]), r["amount_cents"],
            r["payee"] or "", ovr["category_name"], new_cat, cur_name,
        ))
        if args.limit and matched >= args.limit:
            break

    print(f"\n{matched:,} rows match an investment payee override")
    print(f"{skipped_already:,} rows already in an investment category (no-op)")
    print()
    print("By destination:")
    for dest, n in sorted(by_dest.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>5,}  {dest}")

    print("\nSample of planned changes (first 15):")
    for row in plan[:15]:
        rid, dt, amt, payee, new_cat, _, cur_cat = row
        print(f"  #{rid:>6}  {dt}  ${-amt/100:>10,.2f}  "
              f"{payee[:30]:30s}  {cur_cat[:22]:22s} → {new_cat}")

    if not args.apply:
        print("\nDRY-RUN — pass --apply to write.")
        return 0

    print(f"\napplying {len(plan):,} updates…")
    now = datetime.now(timezone.utc)
    with storage.connect(db_path) as con:
        for rid, _, _, _, _, new_cat_id, _ in plan:
            con.execute(
                "UPDATE ledger_txn SET category_id = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new_cat_id, rid),
            )
    storage.audit(db_path, "investment_reclassify_run", {
        "applied": len(plan), "destinations": by_dest,
        "ts": now.isoformat(),
    })
    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
