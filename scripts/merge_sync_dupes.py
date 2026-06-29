"""Merge ledger_txn duplicates created by ynab_full_sync.

A 'dupe pair' is two rows on the same account with the same absolute
amount and posted_date within ±2 days — one synthetic (NULL or
ledger:N ynab_txn_id, came from a CC alert), one real (real UUID, came
from later YNAB sync).

Merge policy:
  * Keep the SYNTHETIC row (it's older, may carry user categorization
    AND the rich parsed-CC-alert memo).
  * Pull the REAL row's data into it:
      - ynab_txn_id  (so writer + future syncs can find it)
      - payee        (YNAB's clean version, e.g. 'Mike Fisher Tours'
                       vs 'MIKE FISHER TOURS-EP')
      - posted_date  (YNAB date is authoritative for accounting)
      - category     (if synthetic had none AND real has one)
      - cleared      (take real's, usually 'cleared' or 'reconciled')
  * Delete the REAL row.
  * Stamp updated_at.

The merge is idempotent — re-running after a partial run only sees
dupes that still exist.

Default dry-run. Re-run with --apply.
"""
from __future__ import annotations
import argparse
import sys

from bot import storage
from bot.config import load_settings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=None,
                        help="cap pairs merged (testing)")
    args = parser.parse_args()
    settings = load_settings()
    db_path = settings.paths.database

    with storage.connect(db_path) as con:
        pairs = con.execute("""
            SELECT
              a.id AS keep_id,
              b.id AS drop_id,
              acc.name AS account_name,
              a.posted_date AS dt_keep,
              b.posted_date AS dt_drop,
              a.amount_cents,
              a.payee AS payee_keep,
              b.payee AS payee_drop,
              a.memo AS memo_keep,
              b.memo AS memo_drop,
              a.category_id AS cat_keep,
              b.category_id AS cat_drop,
              b.ynab_txn_id AS yid_drop,
              b.cleared AS cleared_drop
            FROM ledger_txn a
            JOIN ledger_txn b ON
                  b.account_id = a.account_id
              AND b.amount_cents = a.amount_cents
              AND b.id > a.id
              AND ABS(julianday(b.posted_date) - julianday(a.posted_date)) <= 2
            JOIN account acc ON acc.id = a.account_id
            WHERE a.amount_cents < 0
              AND (a.ynab_txn_id IS NULL OR a.ynab_txn_id LIKE 'ledger:%')
              AND b.ynab_txn_id IS NOT NULL
              AND b.ynab_txn_id NOT LIKE 'ledger:%'
            ORDER BY a.posted_date DESC
        """).fetchall()

    print(f"found {len(pairs)} duplicate pairs to merge\n")
    if args.limit:
        pairs = pairs[: args.limit]

    if not args.apply:
        print("DRY-RUN — pass --apply to write\n")
        for p in pairs[:10]:
            print(f"  KEEP #{p['keep_id']} ({p['dt_keep']}, payee='{p['payee_keep']}'), "
                  f"DROP #{p['drop_id']} ({p['dt_drop']}, payee='{p['payee_drop']}')")
        if len(pairs) > 10:
            print(f"  …and {len(pairs) - 10} more")
        return 0

    print("applying merges…")
    merged = 0
    failed = 0
    for p in pairs:
        try:
            with storage.connect(db_path) as con:
                effective_cat = p["cat_keep"] or p["cat_drop"]
                # Two-step to dodge the UNIQUE constraint on ynab_txn_id:
                # SQLite checks constraints per-statement, not on commit,
                # so we can't UPDATE keep AND have drop still hold the
                # same yid. Park drop's yid under a sentinel first.
                sentinel = f"merged:{p['drop_id']}"
                con.execute(
                    "UPDATE ledger_txn SET ynab_txn_id = ? WHERE id = ?",
                    (sentinel, p["drop_id"]),
                )
                con.execute(
                    "UPDATE ledger_txn SET "
                    "  ynab_txn_id = ?, "
                    "  payee = COALESCE(?, payee), "
                    "  posted_date = ?, "
                    "  category_id = ?, "
                    "  cleared = COALESCE(?, cleared), "
                    "  updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = ?",
                    (p["yid_drop"], p["payee_drop"], p["dt_drop"],
                     effective_cat, p["cleared_drop"], p["keep_id"]),
                )
                con.execute("DELETE FROM ledger_txn WHERE id = ?", (p["drop_id"],))
            merged += 1
        except Exception as e:  # noqa: BLE001
            print(f"  failed pair keep={p['keep_id']} drop={p['drop_id']}: {e}")
            failed += 1

    storage.audit(db_path, "sync_dupes_merged", {
        "merged": merged, "failed": failed, "total_pairs": len(pairs),
    })
    print(f"\nmerged {merged} pairs, {failed} failed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
