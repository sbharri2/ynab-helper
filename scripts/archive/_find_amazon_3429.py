"""Find the $34.29 Chase Amazon charge and its matching Amazon order, if any."""
from bot import storage

with storage.connect("ynab_helper.db") as con:
    print("Pending_txn rows with AMAZON in payee, amount ≈ $34.29:")
    rows = con.execute(
        "SELECT id, txn_date, payee, amount_cents, raw_summary, suggested_category, status "
        "FROM pending_txn "
        "WHERE (payee LIKE '%AMAZON%' OR payee LIKE '%AMZN%') "
        "  AND amount_cents BETWEEN -3500 AND -3300 "
        "ORDER BY txn_date DESC"
    ).fetchall()
    for r in rows:
        amt = (r["amount_cents"] or 0) / 100
        print(f"  #{r['id']}  {r['txn_date']}  ${amt:+.2f}  status={r['status']}  payee={r['payee']!r}")
        print(f"     raw_summary: {(r['raw_summary'] or '')[:120]}")

    print("\nPending_order rows for Amazon around 6/7 with $34.29 total:")
    rows = con.execute(
        "SELECT id, source, order_date, total_cents, status, raw_summary, chosen_category "
        "FROM pending_order "
        "WHERE source IN ('amazon','retailer_order') "
        "  AND total_cents BETWEEN 3300 AND 3500 "
        "ORDER BY order_date DESC"
    ).fetchall()
    for r in rows:
        amt = (r["total_cents"] or 0) / 100
        print(f"  #{r['id']}  {r['source']}  {r['order_date']}  ${amt:.2f}  status={r['status']}  cat={r['chosen_category']}")
        print(f"     raw_summary: {(r['raw_summary'] or '')[:120]}")

    print("\nAll recent Amazon pending_orders (last 14 days, any status):")
    rows = con.execute(
        "SELECT id, order_date, total_cents, status, raw_summary "
        "FROM pending_order "
        "WHERE source = 'amazon' AND order_date >= date('now','-14 days') "
        "ORDER BY order_date DESC LIMIT 15"
    ).fetchall()
    for r in rows:
        amt = (r["total_cents"] or 0) / 100
        print(f"  #{r['id']}  {r['order_date']}  ${amt:>8.2f}  {r['status']:<12} | {(r['raw_summary'] or '')[:70]}")
