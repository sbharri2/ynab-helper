from bot import storage
with storage.connect("ynab_helper.db") as con:
    print("All Amazon pending_orders in last 14 days:")
    rows = con.execute(
        """SELECT id, order_date, total_cents, status, raw_summary, external_id
           FROM pending_order
           WHERE source = 'amazon'
             AND order_date >= date('now', '-14 days')
           ORDER BY order_date"""
    ).fetchall()
    for r in rows:
        d = dict(r)
        print(f"  #{d['id']}  {d['order_date']}  ${d['total_cents']/100:>8.2f}  "
              f"status={d['status']:<12} | ext={d['external_id']}")
        print(f"     {(d['raw_summary'] or '')[:70]}")

    print("\nAll recent ledger_txn for Amazon (last 7 days):")
    rows = con.execute(
        """SELECT id, posted_date, amount_cents, payee, category_id, source_signal
           FROM ledger_txn
           WHERE posted_date >= date('now', '-7 days')
             AND (LOWER(payee) LIKE '%amazon%' OR LOWER(payee) LIKE '%amzn%')
           ORDER BY posted_date"""
    ).fetchall()
    for r in rows:
        d = dict(r)
        cat = (d['category_id'] or '(none)')[:8]
        print(f"  lt#{d['id']}  {d['posted_date']}  ${d['amount_cents']/100:>+8.2f}  "
              f"cat={cat}  src={d['source_signal']}  payee={d['payee']!r}")
