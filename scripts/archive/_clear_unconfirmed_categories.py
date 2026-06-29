"""Clear ledger_txn.category_id for rows whose pending_txn is still 'pending'.

The bug: ingest_signal used to pre-set ledger_txn.category_id from the LLM
suggestion before the user confirmed. As a result, month_category.activity
already counted the charge → confirming via Telegram didn't move the
"$X left this month" line because the math was already including the suggestion.

This script repairs the existing in-flight rows: any ledger_txn with a non-
NULL category_id whose corresponding pending_txn is status='pending' gets
cleared. Then we recompute_month so available_cents reflects only confirmed
activity going forward.

Idempotent. Audit-logged.
"""
from __future__ import annotations
from collections import Counter
from datetime import date

from bot import storage
from bot.envelope import recompute_month

db = "ynab_helper.db"

with storage.connect(db) as con:
    # Find ledger_txn rows linked to pending pending_txns AND currently have
    # a category_id set. These are the auto-suggested-but-unconfirmed rows.
    rows = con.execute(
        """SELECT lt.id AS lt_id, lt.category_id, lt.posted_date,
                  lt.amount_cents, pt.id AS pt_id, pt.payee
           FROM pending_txn pt
           JOIN ledger_txn lt
             ON lt.id = CAST(SUBSTR(pt.ynab_txn_id, 8) AS INTEGER)
           WHERE pt.status = 'pending'
             AND pt.ynab_txn_id LIKE 'ledger:%'
             AND lt.category_id IS NOT NULL"""
    ).fetchall()
    print(f"Found {len(rows)} ledger_txn rows to clear (pending pending_txns)")
    by_cat = Counter()
    months_affected: set[str] = set()
    for r in rows:
        by_cat[r["category_id"]] += 1
        # collect month string
        d = r["posted_date"]
        if isinstance(d, date):
            months_affected.add(d.strftime("%Y-%m"))
        elif isinstance(d, str):
            months_affected.add(d[:7])

    for cid, n in by_cat.most_common(10):
        row = con.execute(
            "SELECT name FROM category WHERE id = ?", (cid,),
        ).fetchone()
        nm = row["name"] if row else cid[:8]
        print(f"  {n:>3}  {nm}")

    if rows:
        con.execute(
            """UPDATE ledger_txn SET category_id = NULL, updated_at = CURRENT_TIMESTAMP
               WHERE id IN (
                 SELECT CAST(SUBSTR(pt.ynab_txn_id, 8) AS INTEGER)
                 FROM pending_txn pt
                 WHERE pt.status = 'pending'
                   AND pt.ynab_txn_id LIKE 'ledger:%'
               )"""
        )
        print(f"cleared {len(rows)} rows")

print(f"\nRecomputing month_category for {len(months_affected)} affected month(s):")
for m in sorted(months_affected):
    res = recompute_month(db, m)
    print(f"  {m}: refreshed {len(res)} categories")

storage.audit(db, "clear_unconfirmed_categories", {
    "rows_cleared": len(rows),
    "months_recomputed": sorted(months_affected),
})
print("done")
