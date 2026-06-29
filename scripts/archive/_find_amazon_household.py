from bot import storage

with storage.connect("ynab_helper.db") as con:
    print("pending_txn with $73.36 or $34.31 in last 2 weeks:")
    rows = con.execute(
        """SELECT id, txn_date, payee, amount_cents, raw_summary, status,
                  ynab_txn_id, suggested_category, chosen_category, chosen_at
           FROM pending_txn
           WHERE (ABS(amount_cents) BETWEEN 7300 AND 7400
                  OR ABS(amount_cents) BETWEEN 3400 AND 3500)
             AND txn_date >= '2026-06-01'
           ORDER BY txn_date"""
    ).fetchall()
    for r in rows:
        amt = (r["amount_cents"] or 0) / 100
        chosen = "✓" if r["chosen_category"] else " "
        print(f"  #{r['id']}  {r['txn_date']}  ${amt:+.2f}  status={r['status']}  chosen={chosen}")
        print(f"     ynab_txn_id   = {r['ynab_txn_id']}")
        print(f"     payee         = {r['payee']!r}")
        print(f"     raw_summary   = {(r['raw_summary'] or '')[:90]}")
        print(f"     chosen_at     = {r['chosen_at']}")
        print()

    print("\nledger_txn rows for $73.36 or $34.31:")
    rows = con.execute(
        """SELECT id, posted_date, payee, amount_cents, category_id, source_signal
           FROM ledger_txn
           WHERE (ABS(amount_cents) BETWEEN 7300 AND 7400
                  OR ABS(amount_cents) BETWEEN 3400 AND 3500)
             AND posted_date >= '2026-06-01'
           ORDER BY posted_date"""
    ).fetchall()
    for r in rows:
        amt = (r["amount_cents"] or 0) / 100
        cat = (r["category_id"] or "(none)")[:8]
        print(f"  #{r['id']}  {r['posted_date']}  ${amt:+.2f}  cat={cat}  src={r['source_signal']}")
        print(f"     payee = {r['payee']!r}")
