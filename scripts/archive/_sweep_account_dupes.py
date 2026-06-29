"""Find all pending_txn duplicate pairs where two rows have the same
(txn_date, amount, payee) but different ledger_txn account_ids.

These are the OLD Joint Checking-style ingest bugs. Reports — does NOT
mutate. Run _fix_venmo_dupe-style fixes manually for each.
"""
from __future__ import annotations
from collections import defaultdict
from bot import storage

with storage.connect("ynab_helper.db") as con:
    rows = con.execute(
        """SELECT pt.id AS pt_id, pt.txn_date, pt.payee, pt.amount_cents,
                  pt.status, pt.ynab_txn_id, lt.id AS lt_id, lt.account_id,
                  a.name AS account_name
           FROM pending_txn pt
           JOIN ledger_txn lt ON lt.id = CAST(SUBSTR(pt.ynab_txn_id, 8) AS INTEGER)
           JOIN account a ON a.id = lt.account_id
           WHERE pt.ynab_txn_id LIKE 'ledger:%'
             AND pt.txn_date >= date('now','-21 days')
           ORDER BY pt.txn_date DESC, pt.amount_cents DESC"""
    ).fetchall()

    # Group by (txn_date, amount_cents, payee)
    groups = defaultdict(list)
    for r in rows:
        key = (str(r["txn_date"])[:10], int(r["amount_cents"] or 0),
               (r["payee"] or "").strip())
        groups[key].append(dict(r))

    n_dupes = 0
    for key, items in groups.items():
        if len(items) < 2:
            continue
        accounts = {i["account_id"] for i in items}
        if len(accounts) < 2:
            # Same account dupe — different issue, skip for this sweep
            continue
        n_dupes += 1
        date_str, amt, payee = key
        print(f"\nDUPE PAIR: {date_str} ${amt/100:+.2f} payee={payee!r}")
        for i in items:
            print(f"  pt#{i['pt_id']:>4}  lt#{i['lt_id']:>5}  "
                  f"status={i['status']:<11}  account={i['account_name']}")

    print(f"\nFound {n_dupes} duplicate pair(s) across different accounts.")
