from bot import storage

with storage.connect("ynab_helper.db") as con:
    print("ledger_txn rows for MassMutual:")
    rows = con.execute(
        """SELECT id, account_id, posted_date, amount_cents, payee, memo,
                  source_signal, source_email_id, dedupe_key, created_at
           FROM ledger_txn
           WHERE LOWER(payee) LIKE '%mass%mutual%' OR LOWER(payee) LIKE '%massmutual%'
              OR LOWER(memo) LIKE '%mass%mutual%' OR LOWER(memo) LIKE '%massmutual%'
           ORDER BY posted_date DESC LIMIT 8"""
    ).fetchall()
    for r in rows:
        d = dict(r)
        print(f"  lt#{d['id']}  {d['posted_date']}  ${d['amount_cents']/100:+.2f}  "
              f"src={d['source_signal']}")
        print(f"     payee = {d['payee']!r}")
        print(f"     memo  = {(d['memo'] or '')[:80]}")
        print(f"     dedupe= {d['dedupe_key']}")
        print()

    print("ledger_signal rows for these:")
    if rows:
        ids = [r["id"] for r in rows]
        placeholders = ",".join(["?"] * len(ids))
        sig_rows = con.execute(
            f"SELECT ledger_txn_id, signal_kind, email_id, parsed_payload "
            f"FROM ledger_signal WHERE ledger_txn_id IN ({placeholders})",
            ids,
        ).fetchall()
        for sr in sig_rows:
            d = dict(sr)
            print(f"  lt#{d['ledger_txn_id']}  kind={d['signal_kind']}  email_id={d['email_id'][:50]}")
            print(f"     parsed (first 200): {(d['parsed_payload'] or '')[:200]}")

    print("\nraw_email_sample matching MassMutual subject/body:")
    rows = con.execute(
        """SELECT id, sender, subject, snippet, inserted_at
           FROM raw_email_sample
           WHERE LOWER(subject) LIKE '%mass%' OR LOWER(body_text) LIKE '%massmutual%'
           ORDER BY id DESC LIMIT 4"""
    ).fetchall()
    for r in rows:
        d = dict(r)
        print(f"  #{d['id']} {d['inserted_at']}  sender={d['sender']}")
        print(f"     subj={d['subject']!r}")
        print(f"     snip={(d['snippet'] or '')[:100]}")
