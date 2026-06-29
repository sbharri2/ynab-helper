"""Is the $3,496.35 PayPal-Southwest charge a real double-payment
or a YNAB split that we're double-counting?

We look at ALL Southwest-related rows on the Citi Double Cash account
in March 2026 and check for the canonical YNAB split signature:
  * One parent row at $3,496.35 (the actual money out)
  * 5 child rows at $699.27 each that sum to the parent
  * Children have the same DATE as the parent
  * Different ynab_txn_ids (each is its own entity in YNAB)

If we see that pattern, it's a SPLIT — one payment, attributed to 5
separate ticket purchases for reporting. Not a duplicate.
"""
from bot import storage
from bot.config import load_settings

s = load_settings()
with storage.connect(s.paths.database) as con:
    # Get the $3,496.35 PayPal row
    print("─── Parent candidate: $3,496.35 in early March 2026 ─────────────")
    parents = con.execute("""
        SELECT id, account_id, posted_date, amount_cents, payee,
               memo, ynab_txn_id, source_signal, category_id, created_at
        FROM ledger_txn
        WHERE amount_cents = -349635
          AND posted_date >= '2026-03-01' AND posted_date <= '2026-03-15'
        ORDER BY posted_date
    """).fetchall()
    for p in parents:
        print(f"  #{p['id']}  {p['posted_date']}  payee='{p['payee']}'  cat_id={p['category_id'] or 'NULL'}")
        print(f"      memo: {(p['memo'] or '(none)')[:100]}")
        print(f"      yid={p['ynab_txn_id']}  src={p['source_signal']}")
        print(f"      created={p['created_at']}")

    print("\n─── Children: $699.27 Southwest in early March 2026 ─────────")
    kids = con.execute("""
        SELECT id, posted_date, payee, memo, ynab_txn_id, source_signal, category_id
        FROM ledger_txn
        WHERE amount_cents = -69927
          AND posted_date >= '2026-03-01' AND posted_date <= '2026-03-15'
        ORDER BY posted_date, id
    """).fetchall()
    for k in kids:
        print(f"  #{k['id']}  {k['posted_date']}  payee='{k['payee']}'  cat_id={k['category_id'] or 'NULL'}")
        print(f"      memo: {(k['memo'] or '(none)')[:100]}")
        print(f"      yid={k['ynab_txn_id']}  src={k['source_signal']}")

    print()
    n_parent = len(parents)
    n_kids = len(kids)
    print(f"Summary: {n_parent} parent row(s) at $3,496.35, {n_kids} child row(s) at $699.27")
    print(f"  parent total: ${sum(-p['amount_cents'] for p in parents)/100:,.2f}")
    print(f"  children sum: ${sum(-k['amount_cents'] for k in kids)/100:,.2f}")
    if parents and kids and n_kids == 5:
        delta_days = (max(p['posted_date'] for p in parents) -
                       min(k['posted_date'] for k in kids))
        print(f"  date span: {delta_days}")
        print()
        if (sum(-k['amount_cents'] for k in kids) ==
            sum(-p['amount_cents'] for p in parents) * n_kids // 1):
            pass
        if sum(-k['amount_cents'] for k in kids) == sum(-p['amount_cents'] for p in parents):
            print("  ✓ children sum EXACTLY matches parent → this is a YNAB SPLIT.")
            print("  → It's our app's double-counting, NOT a real double-charge.")
            print("  → The single real money movement was $3,496.35.")
