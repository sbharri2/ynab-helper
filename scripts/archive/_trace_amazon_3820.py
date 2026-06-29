from bot import storage
from bot.ingest import _is_generic_payee

print("Is 'Amazon.com' generic?")
print(f"  _is_generic_payee('Amazon.com') = {_is_generic_payee('Amazon.com')}")
print(f"  _is_generic_payee('Amazon.com*ABC') = {_is_generic_payee('Amazon.com*ABC')}")

with storage.connect("ynab_helper.db") as con:
    print("\npending_txn $38.20 6/15:")
    rows = con.execute(
        "SELECT id, txn_date, payee, amount_cents, raw_summary, "
        "suggested_category, status, created_at, ynab_txn_id "
        "FROM pending_txn "
        "WHERE amount_cents BETWEEN -3900 AND -3700 "
        "  AND txn_date BETWEEN '2026-06-14' AND '2026-06-17' "
        "  AND (payee LIKE '%AMAZON%' OR payee LIKE '%amazon%' OR payee LIKE '%AMZN%')"
    ).fetchall()
    for r in rows:
        print(f"  {dict(r)}")

    print("\npending_order for $38.20 near 6/15:")
    rows = con.execute(
        "SELECT id, source, order_date, total_cents, status, raw_summary, "
        "chosen_category, external_id "
        "FROM pending_order "
        "WHERE total_cents BETWEEN 3700 AND 3900 "
        "  AND order_date BETWEEN '2026-06-13' AND '2026-06-17'"
    ).fetchall()
    for r in rows:
        d = dict(r)
        print(f"  #{d['id']}  {d['source']}  {d['order_date']}  ${d['total_cents']/100:.2f}  "
              f"status={d['status']}  cat={(d['chosen_category'] or '(none)')[:8]}")
        print(f"     {(d['raw_summary'] or '')[:80]}")

    print("\nAudit around the pending_txn's creation:")
    if rows:
        pass
