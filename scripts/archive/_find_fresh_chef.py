from bot import storage
with storage.connect("ynab_helper.db") as con:
    print("All pending_txn for Fresh Chef:")
    rows = con.execute(
        """SELECT id, txn_date, payee, amount_cents, status, raw_summary,
                  ynab_txn_id, suggested_category, chosen_category, chosen_at
           FROM pending_txn
           WHERE payee LIKE '%Fresh Chef%' OR raw_summary LIKE '%Fresh Chef%'
           ORDER BY txn_date"""
    ).fetchall()
    for r in rows:
        amt = (r["amount_cents"] or 0) / 100
        print(f"  #{r['id']}  {r['txn_date']}  ${amt:+.2f}  status={r['status']}")
        print(f"     ynab_txn_id   = {r['ynab_txn_id']}")
        print(f"     payee         = {r['payee']!r}")
        print(f"     raw_summary   = {(r['raw_summary'] or '')[:90]}")
        print(f"     chosen_at     = {r['chosen_at']}")
        print(f"     chosen_cat    = {(r['chosen_category'] or '(none)')[:8]}")
        print()

    print("Ledger_txn for Fresh Chef:")
    rows = con.execute(
        """SELECT id, posted_date, payee, amount_cents, category_id,
                  source_signal, ynab_txn_id
           FROM ledger_txn
           WHERE payee LIKE '%Fresh Chef%'
           ORDER BY posted_date"""
    ).fetchall()
    for r in rows:
        amt = (r["amount_cents"] or 0) / 100
        cat = (r["category_id"] or "(none)")[:8]
        print(f"  #{r['id']}  {r['posted_date']}  ${amt:+.2f}  cat={cat}  "
              f"src={r['source_signal']}  ynab_id={r['ynab_txn_id']}")
