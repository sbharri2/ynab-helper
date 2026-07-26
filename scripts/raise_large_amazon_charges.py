"""Raise already-filed Amazon charges at/above the large-charge threshold.

Clears ledger_txn.category_id, enqueues a COLD pending_txn so the row shows
up in the Inbox, and refreshes ONLY the affected bucket categories' cached
month_category rows.

Uses apply_activity_delta (additive, anchor-preserving, scoped to the given
categories) not recompute_month — the latter rebuilds `available` from the
identity and trampled same-month anchor writes on 2026-07-25 (see
bot/envelope.py:301).

Usage:
    .venv/Scripts/python.exe scripts/raise_large_amazon_charges.py --dry-run
    .venv/Scripts/python.exe scripts/raise_large_amazon_charges.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-06-01",
                    help="only touch charges posted on/after this date")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from dotenv import load_dotenv
    repo = Path(__file__).resolve().parents[1]
    load_dotenv(repo / ".env", override=True)

    from bot import envelope, ingest, storage
    from bot.config import load_settings

    settings = load_settings(repo / "config.yaml")
    db = settings.paths.database

    with storage.connect(db) as con:
        rows = [dict(r) for r in con.execute(
            """SELECT lt.id, lt.posted_date, lt.payee, lt.amount_cents,
                      lt.account_id, lt.category_id, c.name AS cat_name
               FROM ledger_txn lt
               JOIN category c ON c.id = lt.category_id
               WHERE c.name LIKE 'Amazon - %'
                 AND lt.posted_date >= ?
                 AND lt.is_split = 0""",
            (args.since,),
        )]

    targets = [r for r in rows
               if ingest._is_large_amazon_charge(r["amount_cents"], settings)]
    if not targets:
        print("nothing to raise.")
        return 0

    touched_categories: set[tuple[str, str]] = set()
    for r in targets:
        month = str(r["posted_date"])[:7]
        print(f"  lt#{r['id']}  {r['posted_date']}  "
              f"${r['amount_cents'] / 100:>10,.2f}  {r['payee']}  "
              f"[{r['cat_name']}] -> Inbox")
        touched_categories.add((month, r["category_id"]))
        if args.dry_run:
            continue

        with storage.connect(db) as con:
            con.execute(
                "UPDATE ledger_txn SET category_id = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (r["id"],),
            )
        pt_id = storage.insert_pending_txn(
            db,
            user_id="steven",
            ynab_txn_id=f"ledger:{r['id']}",
            ynab_account_id=r["account_id"],
            payee=r["payee"],
            amount_cents=r["amount_cents"],
            txn_date=r["posted_date"],
            memo=f"raised for categorization (was {r['cat_name']})",
        )
        if pt_id:
            with storage.connect(db) as con:
                con.execute(
                    "UPDATE pending_txn SET queue_lane = 'cold' WHERE id = ?",
                    (pt_id,),
                )
        storage.audit(db, "large_amazon_raised", {
            "ledger_txn_id": r["id"], "pending_txn_id": pt_id,
            "was_category": r["cat_name"], "amount_cents": r["amount_cents"],
        })

    if args.dry_run:
        print(f"\n[dry-run] would raise {len(targets)} charge(s); "
              f"would refresh {len(touched_categories)} month/category pair(s).")
        return 0

    by_month: dict[str, list[str]] = {}
    for month, category_id in touched_categories:
        by_month.setdefault(month, []).append(category_id)
    for month, category_ids in sorted(by_month.items()):
        envelope.apply_activity_delta(db, month, category_ids)
        print(f"  refreshed {month}: {len(category_ids)} categor(ies)")

    print(f"\nraised {len(targets)} charge(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
