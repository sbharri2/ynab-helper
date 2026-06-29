from bot import storage
with storage.connect("ynab_helper.db") as con:
    print("Ledger 23610 + 23622:")
    rows = con.execute(
        "SELECT id, account_id, posted_date, amount_cents, payee, "
        "       category_id, source_signal, source_email_id, dedupe_key "
        "FROM ledger_txn WHERE id IN (23610, 23622)"
    ).fetchall()
    for r in rows:
        d = dict(r)
        print(f"  #{d['id']}  acct={d['account_id'][:8]}  {d['posted_date']}  "
              f"${d['amount_cents']/100:+.2f}  src={d['source_signal']}")
        print(f"     payee     = {d['payee']!r}")
        print(f"     dedupe_key= {d['dedupe_key']}")
        print(f"     email_id  = {d['source_email_id']}")
        print()

    print("All ledger_signal rows for these txns:")
    rows = con.execute(
        "SELECT ledger_txn_id, signal_kind, email_id, received_at "
        "FROM ledger_signal WHERE ledger_txn_id IN (23610, 23622) "
        "ORDER BY ledger_txn_id, id"
    ).fetchall()
    for r in rows:
        print(f"  lt={r['ledger_txn_id']}  kind={r['signal_kind']:<25}  "
              f"email={r['email_id'][:50]}  rcvd={r['received_at']}")

    print("\nAccount details for the two ids:")
    aids = set()
    for r in con.execute("SELECT DISTINCT account_id FROM ledger_txn WHERE id IN (23610, 23622)"):
        aids.add(r["account_id"])
    for aid in aids:
        r = con.execute(
            "SELECT id, name, type, last4 FROM account WHERE id = ?", (aid,)
        ).fetchone()
        print(f"  {dict(r)}")
