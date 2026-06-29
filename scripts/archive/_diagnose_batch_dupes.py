"""Diagnose the two issues in today's /batch view:
  1. Why does Amazon $42.89 6/18 have no enrichment?
  2. Why do items appear in apparent duplicates (#4 vs #10, #13 vs #14, etc.)
"""
from bot import storage

with storage.connect("ynab_helper.db") as con:
    print("=" * 78)
    print("ISSUE 1: Amazon $42.89 6/18")
    print("=" * 78)
    rows = con.execute(
        """SELECT id, payee, amount_cents, txn_date, raw_summary, ynab_txn_id,
                  suggested_category, queue_lane, status, created_at
           FROM pending_txn
           WHERE ABS(amount_cents) = 4289 AND txn_date BETWEEN '2026-06-15' AND '2026-06-20'"""
    ).fetchall()
    for r in rows:
        d = dict(r)
        print(f"  pt#{d['id']}  {d['txn_date']}  ${d['amount_cents']/100:+.2f}  "
              f"lane={d['queue_lane']}  status={d['status']}")
        print(f"     payee={d['payee']!r}")
        print(f"     raw_summary={(d['raw_summary'] or '')[:80]!r}")
        print(f"     ynab_id={d['ynab_txn_id']}")
        print(f"     created={d['created_at']}")

    # Look for matching pending_order
    print("\n  Matching Amazon pending_orders around 6/15-6/20 with $42-$50:")
    rows = con.execute(
        "SELECT id, order_date, total_cents, status, raw_summary, external_id "
        "FROM pending_order "
        "WHERE source='amazon' AND order_date BETWEEN '2026-06-10' AND '2026-06-20' "
        "  AND total_cents BETWEEN 3500 AND 5500"
    ).fetchall()
    for r in rows:
        d = dict(r)
        print(f"    po#{d['id']}  {d['order_date']}  ${d['total_cents']/100:.2f}  "
              f"status={d['status']}  ext_id={d['external_id']}")
        print(f"       {(d['raw_summary'] or '')[:80]}")

    print()
    print("=" * 78)
    print("ISSUE 2: Apparent duplicates in the queue")
    print("=" * 78)
    # Find every pending row, group by (date, amount), show rows with >1
    rows = con.execute(
        """SELECT id, payee, amount_cents, txn_date, ynab_txn_id, suggested_category,
                  ynab_account_id, status, queue_lane
           FROM pending_txn
           WHERE status = 'pending' AND queue_lane = 'cold'
           ORDER BY txn_date, ABS(amount_cents)"""
    ).fetchall()
    rows = [dict(r) for r in rows]
    from collections import defaultdict
    by_key = defaultdict(list)
    for r in rows:
        key = (str(r["txn_date"])[:10], int(r["amount_cents"]))
        by_key[key].append(r)

    dupe_groups = [(k, v) for k, v in by_key.items() if len(v) > 1]
    print(f"\nFound {len(dupe_groups)} (date, amount) groups with >1 row:")
    for key, group in dupe_groups:
        d, amt = key
        print(f"\n  {d}  ${amt/100:+.2f}")
        for r in group:
            print(f"    pt#{r['id']}  payee={r['payee']!r}")
            print(f"       ynab_id={r['ynab_txn_id']}  acct={r['ynab_account_id'][:8]}")
