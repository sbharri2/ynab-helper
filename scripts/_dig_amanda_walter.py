"""Dig up everything the bot knows about Amanda Walter / Punta Cana."""
from bot import storage
from bot.config import load_settings

s = load_settings()
with storage.connect(s.paths.database) as con:
    print("─── ALL ledger_txn matching Amanda Walter ─────────────────")
    rows = con.execute("""
        SELECT id, account_id, posted_date, amount_cents, payee, memo,
               category_id, source_signal, ynab_txn_id, created_at, updated_at
        FROM ledger_txn
        WHERE LOWER(payee) LIKE '%amanda walter%'
           OR LOWER(memo) LIKE '%amanda walter%'
        ORDER BY posted_date DESC
    """).fetchall()
    for r in rows:
        print(f"  #{r['id']}  {r['posted_date']}  ${-r['amount_cents']/100:>8,.2f}  "
              f"payee='{r['payee']}'  src={r['source_signal']}  cat={r['category_id'] or 'NULL'}")
        print(f"      memo: {(r['memo'] or '')[:200]}")
        print(f"      yid: {r['ynab_txn_id']}  created={r['created_at']}")

    print("\n─── ALL ledger_signal rows tied to ledger #?  ─────────────")
    if rows:
        for r in rows:
            sigs = con.execute(
                "SELECT signal_kind, email_id, parsed_payload "
                "FROM ledger_signal WHERE ledger_txn_id = ?",
                (r['id'],)
            ).fetchall()
            print(f"  ledger_txn #{r['id']}:")
            for sig in sigs:
                print(f"    {sig['signal_kind']}  email_id={sig['email_id']}")
                if sig['parsed_payload']:
                    import json as _j
                    try:
                        pl = _j.loads(sig['parsed_payload'])
                        for k, v in pl.items():
                            print(f"      {k}: {str(v)[:80]}")
                    except Exception:
                        print(f"      raw: {sig['parsed_payload'][:200]}")

    print("\n─── ALL ledger_txn mentioning Punta Cana / Punta Blanca ───")
    rows = con.execute("""
        SELECT id, posted_date, amount_cents, payee, memo, source_signal, category_id
        FROM ledger_txn
        WHERE LOWER(payee) LIKE '%punta%'
           OR LOWER(memo) LIKE '%punta%'
        ORDER BY posted_date DESC
    """).fetchall()
    for r in rows:
        cat_name = ""
        if r['category_id']:
            c = con.execute("SELECT name FROM category WHERE id = ?", (r['category_id'],)).fetchone()
            cat_name = c['name'] if c else r['category_id']
        print(f"  #{r['id']}  {r['posted_date']}  ${-r['amount_cents']/100:>9,.2f}  "
              f"payee='{(r['payee'] or '')[:30]:30s}'  cat='{cat_name[:30]}'  src={r['source_signal']}")
        if r['memo']:
            print(f"      memo: {r['memo'][:150]}")

    print("\n─── Other Venmo activity in Jun 2026 (same week) ─────────")
    rows = con.execute("""
        SELECT id, posted_date, amount_cents, payee, memo, source_signal, category_id
        FROM ledger_txn
        WHERE LOWER(payee) LIKE '%venmo%'
          AND posted_date BETWEEN '2026-06-10' AND '2026-06-25'
        ORDER BY posted_date DESC
    """).fetchall()
    for r in rows:
        cat = ""
        if r['category_id']:
            c = con.execute("SELECT name FROM category WHERE id = ?", (r['category_id'],)).fetchone()
            cat = c['name'] if c else ''
        print(f"  #{r['id']}  {r['posted_date']}  ${-r['amount_cents']/100:>8,.2f}  "
              f"payee='{(r['payee'] or '')[:25]:25s}'  cat='{cat[:25]}'  src={r['source_signal']}")
        if r['memo']:
            print(f"      memo: {r['memo'][:150]}")

    print("\n─── raw_email_sample matching Amanda Walter or Punta ─────")
    rows = con.execute("""
        SELECT id, subject, sender, date_header, snippet
        FROM raw_email_sample
        WHERE LOWER(subject) LIKE '%amanda walter%'
           OR LOWER(snippet) LIKE '%amanda walter%'
           OR LOWER(snippet) LIKE '%punta cana%'
           OR LOWER(snippet) LIKE '%punta blanca%'
        ORDER BY internal_date DESC
        LIMIT 10
    """).fetchall()
    for r in rows:
        print(f"  #{r['id']}  {r['date_header']}  from={(r['sender'] or '')[:30]}")
        print(f"      subject: {(r['subject'] or '')[:80]}")
        print(f"      snippet: {(r['snippet'] or '')[:150]}")
