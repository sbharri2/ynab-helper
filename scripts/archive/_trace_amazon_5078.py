from bot import storage
with storage.connect("ynab_helper.db") as con:
    print("pt rows for $50.78 6/20-6/23:")
    rows = con.execute(
        "SELECT id, txn_date, payee, amount_cents, raw_summary, "
        "       suggested_category, queue_lane, status, ynab_txn_id "
        "FROM pending_txn "
        "WHERE ABS(amount_cents) = 5078 AND txn_date BETWEEN '2026-06-20' AND '2026-06-23'"
    ).fetchall()
    for r in rows:
        d = dict(r)
        print(f"  pt#{d['id']}  {d['txn_date']}  ${d['amount_cents']/100:+.2f}  "
              f"status={d['status']:<11} lane={d['queue_lane']}")
        print(f"     payee = {d['payee']!r}")
        print(f"     raw   = {(d['raw_summary'] or '')[:90]!r}")
        print(f"     yid   = {d['ynab_txn_id']}")

    print("\nAmazon pending_orders with $50.78 (±5% = $48-53) around 6/18-6/24:")
    rows = con.execute(
        "SELECT id, source, order_date, total_cents, status, raw_summary, external_id "
        "FROM pending_order "
        "WHERE source='amazon' "
        "  AND order_date BETWEEN '2026-06-18' AND '2026-06-24' "
        "  AND total_cents BETWEEN 4800 AND 5400"
    ).fetchall()
    for r in rows:
        d = dict(r)
        print(f"  po#{d['id']}  {d['order_date']}  ${d['total_cents']/100:.2f}  "
              f"status={d['status']}  ext={d['external_id']}")
        print(f"     {(d['raw_summary'] or '')[:80]}")

    print("\nAll Amazon orders in window (any amount):")
    rows = con.execute(
        "SELECT id, source, order_date, total_cents, status, raw_summary, external_id "
        "FROM pending_order "
        "WHERE source='amazon' AND order_date BETWEEN '2026-06-18' AND '2026-06-24'"
    ).fetchall()
    for r in rows:
        d = dict(r)
        print(f"  po#{d['id']}  {d['order_date']}  ${d['total_cents']/100:.2f}  "
              f"status={d['status']}  ext={d['external_id']}")
        print(f"     {(d['raw_summary'] or '')[:80]}")
