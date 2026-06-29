"""Retroactively propagate chosen_category from recently-categorized
pending_orders to their matching ledger_txn rows.

Why: until the just-deployed fix, mark_order_categorized only touched
pending_order; the matching ledger_txn (from the CC alert) stayed
category_id=NULL. As a result the "$X left in pot" line didn't reflect
those orders.
"""
from __future__ import annotations
from bot import storage
from bot.envelope import recompute_month
from bot.telegram_bot import _propagate_order_category_to_ledger

with storage.connect("ynab_helper.db") as con:
    rows = con.execute(
        """SELECT id, source, order_date, total_cents, chosen_category, raw_summary
           FROM pending_order
           WHERE status = 'categorized'
             AND chosen_category IS NOT NULL
             AND chosen_at >= datetime('now', '-2 days')
           ORDER BY chosen_at DESC"""
    ).fetchall()
    orders = [dict(r) for r in rows]

print(f"Found {len(orders)} recently-categorized orders to propagate.")
for o in orders:
    print(f"  #{o['id']}  {o['source']}  {o['order_date']}  ${o['total_cents']/100:.2f}  "
          f"-> cat={o['chosen_category'][:8]}  {(o['raw_summary'] or '')[:50]}")
    _propagate_order_category_to_ledger(
        "ynab_helper.db", o, o["chosen_category"],
    )

# Now refresh month_category for affected month(s)
months_to_recompute = set()
for o in orders:
    od = o["order_date"]
    if hasattr(od, "year"):
        months_to_recompute.add(od.strftime("%Y-%m"))
    else:
        months_to_recompute.add(str(od)[:7])

print(f"\nRecomputing months: {sorted(months_to_recompute)}")
for m in sorted(months_to_recompute):
    res = recompute_month("ynab_helper.db", m)
    print(f"  {m}: refreshed {len(res)} categories")

print("\ndone")
