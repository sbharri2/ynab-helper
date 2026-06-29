from bot import storage
with storage.connect("ynab_helper.db") as con:
    print("ledger_txn 23650 vs 23818:")
    for lid in (23650, 23818):
        r = con.execute(
            "SELECT id, account_id, posted_date, amount_cents, payee, memo, "
            "category_id, source_signal, source_email_id, created_at "
            "FROM ledger_txn WHERE id = ?", (lid,),
        ).fetchone()
        if not r:
            print(f"  lt#{lid}: NOT FOUND")
            continue
        d = dict(r)
        print(f"\n  lt#{d['id']}:")
        print(f"     account_id    = {d['account_id'][:8]}")
        print(f"     posted_date   = {d['posted_date']}")
        print(f"     amount        = ${d['amount_cents']/100:+.2f}")
        print(f"     payee         = {d['payee']!r}")
        print(f"     memo          = {(d['memo'] or '')[:80]!r}")
        print(f"     source_signal = {d['source_signal']}")
        print(f"     source_email  = {d['source_email_id']}")
        print(f"     created_at    = {d['created_at']}")

    print("\nledger_signal rows referencing these ledger txns:")
    rows = con.execute(
        "SELECT ledger_txn_id, signal_kind, email_id, received_at "
        "FROM ledger_signal WHERE ledger_txn_id IN (23650, 23818) "
        "ORDER BY ledger_txn_id, id"
    ).fetchall()
    for r in rows:
        print(f"  lt#{r['ledger_txn_id']}  kind={r['signal_kind']}  rcvd={r['received_at']}")
        print(f"     email_id = {r['email_id']}")
