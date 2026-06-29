from bot import storage
with storage.connect("ynab_helper.db") as con:
    rows = con.execute(
        "SELECT id, txn_date, payee, amount_cents, status, queue_lane, suggested_category, last_pushed_at "
        "FROM pending_txn WHERE LOWER(payee) LIKE '%duke%' "
        "ORDER BY id DESC LIMIT 5"
    ).fetchall()
    for r in rows:
        d = dict(r)
        cn = "(none)"
        if d["suggested_category"]:
            c = con.execute("SELECT name FROM category WHERE id = ?", (d["suggested_category"],)).fetchone()
            if c:
                cn = c["name"]
        print(f"  pt#{d['id']} {d['txn_date']} ${d['amount_cents']/100:+.2f} "
              f"status={d['status']:<11} lane={d['queue_lane']:<6} sug={cn}  "
              f"pushed={d['last_pushed_at']}")
        print(f"     payee={d['payee']!r}")
