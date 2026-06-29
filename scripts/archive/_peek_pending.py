from bot import storage
with storage.connect("ynab_helper.db") as con:
    r = con.execute("SELECT * FROM pending_order WHERE id = 27").fetchone()
    if r:
        d = dict(r)
        print(f"Order #27 ({d['source']}):")
        print(f"  total: ${(d['total_cents'] or 0)/100:.2f}")
        print(f"  date:  {d['order_date']}")
        print(f"  summary: {d['raw_summary']}")
