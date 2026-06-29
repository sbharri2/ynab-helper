"""Retroactively sync chosen_category from categorized pending_txns
(real YNAB id, not 'ledger:N') down to their matching local ledger_txn rows.

Until the fix in _apply_choice, the YNAB-id path called ynab.set_category
but didn't touch ledger_txn. The local activity_cents math then missed
the change until the next ynab_watcher poll.
"""
from __future__ import annotations
from bot import storage
from bot.envelope import recompute_month

with storage.connect("ynab_helper.db") as con:
    rows = con.execute(
        """SELECT pt.id, pt.ynab_txn_id, pt.chosen_category, pt.amount_cents,
                  pt.txn_date, pt.payee, lt.id AS lt_id, lt.category_id AS lt_cat
           FROM pending_txn pt
           LEFT JOIN ledger_txn lt ON lt.ynab_txn_id = pt.ynab_txn_id
           WHERE pt.status = 'categorized'
             AND pt.ynab_txn_id NOT LIKE 'ledger:%'
             AND pt.chosen_at >= datetime('now', '-1 day')
             AND (lt.category_id IS NULL OR lt.category_id != pt.chosen_category)
           ORDER BY pt.chosen_at DESC"""
    ).fetchall()
    rows = [dict(r) for r in rows]

print(f"Rows to fix: {len(rows)}")
months_to_recompute = set()

for r in rows:
    print(f"  pt#{r['id']}  ${r['amount_cents']/100:+.2f}  "
          f"chosen={r['chosen_category'][:8]}  lt#{r['lt_id']}  "
          f"lt_cat_was={(r['lt_cat'] or 'NULL')[:8]}  {(r['payee'] or '')[:25]}")

if rows:
    with storage.connect("ynab_helper.db") as con:
        for r in rows:
            con.execute(
                "UPDATE ledger_txn SET category_id = ?, "
                "updated_at = CURRENT_TIMESTAMP "
                "WHERE ynab_txn_id = ?",
                (r["chosen_category"], r["ynab_txn_id"]),
            )
            d = r["txn_date"]
            mstr = (d.strftime("%Y-%m")
                    if hasattr(d, "strftime")
                    else str(d)[:7])
            months_to_recompute.add(mstr)

    print(f"\nRecomputing months: {sorted(months_to_recompute)}")
    for m in sorted(months_to_recompute):
        res = recompute_month("ynab_helper.db", m)
        print(f"  {m}: refreshed {len(res)} categories")

storage.audit("ynab_helper.db", "ynab_id_ledger_resync", {
    "rows_fixed": len(rows),
    "months": sorted(months_to_recompute),
})
print("\ndone")
