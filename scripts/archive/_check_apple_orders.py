from bot import storage
with storage.connect("ynab_helper.db") as con:
    rows = con.execute(
        "SELECT id, source, order_date, total_cents, status, raw_summary "
        "FROM pending_order WHERE source = 'apple' ORDER BY id"
    ).fetchall()
    print(f"{len(rows)} apple pending_orders:")
    for r in rows:
        amt = (r["total_cents"] or 0) / 100
        print(f"  #{r['id']} {r['order_date']} ${amt:.2f} status={r['status']}")
        print(f"     summary: {(r['raw_summary'] or '')[:80]}")
