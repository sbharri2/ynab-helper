from bot import storage
with storage.connect("ynab_helper.db") as con:
    print("All ledger_txn for MASSMUTUAL / MASSACHUSETTS (last 2 days):")
    rows = con.execute(
        """SELECT id, posted_date, amount_cents, payee, source_signal, source_email_id
           FROM ledger_txn
           WHERE created_at >= datetime('now', '-1 day')
             AND (LOWER(payee) LIKE '%mass%' OR LOWER(memo) LIKE '%mass%')
           ORDER BY id"""
    ).fetchall()
    if not rows:
        print("  (none in last 24h)")
    for r in rows:
        d = dict(r)
        print(f"  lt#{d['id']}  {d['posted_date']}  ${d['amount_cents']/100:+.2f}  "
              f"payee={d['payee']!r}  email_id={d['source_email_id'][:50]}")
