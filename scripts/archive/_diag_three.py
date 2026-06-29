"""Address Steven's three concerns:
  1. Mochi history → what's the right category
  2. Einstein Bagels appearing 2x in the queue
  3. Amazon $23.58 6/19 has no detail
"""
from bot import storage

with storage.connect("ynab_helper.db") as con:
    cat_name_by_id = {r["id"]: r["name"]
                      for r in con.execute("SELECT id, name FROM category").fetchall()}

    print("=" * 78)
    print("1. MOCHI history — what was it categorized as before?")
    print("=" * 78)
    rows = con.execute(
        "SELECT id, posted_date, amount_cents, payee, category_id "
        "FROM ledger_txn WHERE LOWER(payee) LIKE '%mochi%' "
        "ORDER BY posted_date DESC LIMIT 15"
    ).fetchall()
    for r in rows:
        d = dict(r)
        cn = cat_name_by_id.get(d['category_id'], '(none)') if d['category_id'] else '(none)'
        print(f"  lt#{d['id']:>5}  {d['posted_date']}  ${d['amount_cents']/100:+8.2f}  "
              f"cat={cn[:30]:<30}  payee={d['payee']!r}")

    # Also categorized pending_txns
    print("\n  pending_txn history:")
    rows = con.execute(
        "SELECT id, txn_date, payee, amount_cents, chosen_category, status "
        "FROM pending_txn WHERE LOWER(payee) LIKE '%mochi%' "
        "ORDER BY txn_date DESC LIMIT 10"
    ).fetchall()
    for r in rows:
        d = dict(r)
        cn = cat_name_by_id.get(d['chosen_category'], '(none)') if d['chosen_category'] else '(none)'
        print(f"  pt#{d['id']:>4}  {d['txn_date']}  ${d['amount_cents']/100:+.2f}  "
              f"status={d['status']:<12}  chosen={cn}  payee={d['payee']!r}")

    print("\n" + "=" * 78)
    print("2. EINSTEIN BAGELS — current pending queue check")
    print("=" * 78)
    rows = con.execute(
        "SELECT id, txn_date, payee, amount_cents, ynab_txn_id, status, "
        "       queue_lane, suggested_category "
        "FROM pending_txn WHERE LOWER(payee) LIKE '%einstein%' "
        "ORDER BY txn_date DESC"
    ).fetchall()
    for r in rows:
        d = dict(r)
        cn = cat_name_by_id.get(d['suggested_category'], '(none)') if d['suggested_category'] else '(none)'
        print(f"  pt#{d['id']:>4}  {d['txn_date']}  ${d['amount_cents']/100:+.2f}  "
              f"status={d['status']:<11}  lane={d['queue_lane']}  sug={cn}")
        print(f"     payee     = {d['payee']!r}")
        print(f"     ynab_id   = {d['ynab_txn_id']}")

    print("\n" + "=" * 78)
    print("3. AMAZON $23.58 6/19 detail")
    print("=" * 78)
    rows = con.execute(
        "SELECT id, txn_date, payee, amount_cents, raw_summary, ynab_txn_id, "
        "       suggested_category, status, queue_lane "
        "FROM pending_txn "
        "WHERE ABS(amount_cents) = 2358 AND txn_date BETWEEN '2026-06-17' AND '2026-06-22'"
    ).fetchall()
    for r in rows:
        d = dict(r)
        cn = cat_name_by_id.get(d['suggested_category'], '(none)') if d['suggested_category'] else '(none)'
        print(f"  pt#{d['id']}  {d['txn_date']}  ${d['amount_cents']/100:+.2f}  "
              f"status={d['status']}  lane={d['queue_lane']}")
        print(f"     payee = {d['payee']!r}")
        print(f"     raw   = {(d['raw_summary'] or '')[:80]!r}")
        print(f"     yid   = {d['ynab_txn_id']}")
        print(f"     sug   = {cn}")
    print()
    print("  Amazon pending_orders around 6/17-6/22 with $23-$28:")
    rows = con.execute(
        "SELECT id, order_date, total_cents, status, raw_summary, external_id "
        "FROM pending_order "
        "WHERE source='amazon' AND order_date BETWEEN '2026-06-15' AND '2026-06-22' "
        "  AND total_cents BETWEEN 2200 AND 2800"
    ).fetchall()
    for r in rows:
        d = dict(r)
        print(f"    po#{d['id']}  {d['order_date']}  ${d['total_cents']/100:.2f}  "
              f"status={d['status']}  ext_id={d['external_id']}")
        print(f"       {(d['raw_summary'] or '')[:80]}")
