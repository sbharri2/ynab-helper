from bot import storage

with storage.connect("ynab_helper.db") as con:
    print("pending_txn rows for $90 6/8-ish, any status:")
    rows = con.execute(
        """SELECT id, txn_date, payee, amount_cents, raw_summary, status,
                  ynab_txn_id, suggested_category, chosen_category, last_pushed_at
           FROM pending_txn
           WHERE amount_cents BETWEEN 8500 AND 9500
             AND txn_date BETWEEN '2026-06-07' AND '2026-06-09'
           ORDER BY id"""
    ).fetchall()
    for r in rows:
        amt = (r["amount_cents"] or 0) / 100
        print(f"  #{r['id']}  {r['txn_date']}  ${amt:+.2f}  status={r['status']}")
        print(f"     ynab_txn_id   = {r['ynab_txn_id']}")
        print(f"     payee         = {r['payee']!r}")
        print(f"     raw_summary   = {(r['raw_summary'] or '')[:90]}")
        print(f"     suggested cat = {(r['suggested_category'] or '(none)')[:8]}")
        print(f"     chosen cat    = {(r['chosen_category'] or '(none)')[:8]}")
        print(f"     last_pushed   = {r['last_pushed_at']}")
        print()

    print("\npending_order rows for $90 6/8-ish:")
    rows = con.execute(
        """SELECT id, source, order_date, total_cents, status, raw_summary, chosen_category
           FROM pending_order
           WHERE total_cents BETWEEN 8500 AND 9500
             AND order_date BETWEEN '2026-06-07' AND '2026-06-09'
           ORDER BY id"""
    ).fetchall()
    for r in rows:
        amt = (r["total_cents"] or 0) / 100
        print(f"  #{r['id']}  {r['source']}  {r['order_date']}  ${amt:.2f}  status={r['status']}  cat={(r['chosen_category'] or '(none)')[:8]}")
        print(f"     summary: {(r['raw_summary'] or '')[:80]}")
