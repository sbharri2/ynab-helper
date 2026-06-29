from bot import storage
with storage.connect("ynab_helper.db") as con:
    print("All ledger_txn for $79.88 around 6/14-6/18:")
    rows = con.execute(
        """SELECT id, posted_date, amount_cents, payee, source_signal,
                  source_email_id, created_at
           FROM ledger_txn
           WHERE ABS(amount_cents) = 7988
             AND posted_date BETWEEN '2026-06-14' AND '2026-06-18'"""
    ).fetchall()
    for r in rows:
        d = dict(r)
        print(f"  lt#{d['id']}  {d['posted_date']}  ${d['amount_cents']/100:+.2f}  "
              f"payee={d['payee']!r}  src={d['source_signal']}  "
              f"created={d['created_at']}")
        print(f"     email_id={(d['source_email_id'] or '')[:50]}")
