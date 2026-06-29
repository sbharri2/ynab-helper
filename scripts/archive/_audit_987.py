from bot import storage
with storage.connect("ynab_helper.db") as con:
    print("Audit around 2026-06-16 20:20 (when #987 was created):")
    rows = con.execute(
        "SELECT id, event, details, ts FROM audit_log "
        "WHERE ts BETWEEN '2026-06-16 20:19:00' AND '2026-06-16 20:23:00' "
        "ORDER BY id"
    ).fetchall()
    for r in rows:
        print(f"  #{r['id']} {str(r['ts'])[:19]} {r['event']:<28} {(r['details'] or '')[:130]}")

    print("\nAll categorize_via_prior events for Mosquito:")
    rows = con.execute(
        "SELECT id, event, details, ts FROM audit_log "
        "WHERE details LIKE '%MOSQUITO%' AND event LIKE 'categorize%' "
        "ORDER BY id"
    ).fetchall()
    for r in rows:
        print(f"  #{r['id']} {r['ts']}: {(r['details'] or '')[:130]}")

    print("\nAll pending_txns created on/after 6/16 with no suggested_category:")
    rows = con.execute(
        "SELECT COUNT(*) AS n FROM pending_txn "
        "WHERE created_at >= '2026-06-16' AND suggested_category IS NULL "
        "  AND status = 'pending'"
    ).fetchone()
    print(f"  count: {rows['n']}")

    print("\nSample 8 NULL-suggestion pendings:")
    rows = con.execute(
        "SELECT id, txn_date, payee, amount_cents, status, created_at, ynab_txn_id "
        "FROM pending_txn "
        "WHERE created_at >= '2026-06-16' AND suggested_category IS NULL "
        "  AND status = 'pending' "
        "ORDER BY id"
    ).fetchall()
    for r in rows:
        d = dict(r)
        print(f"  #{d['id']}  {d['txn_date']}  ${d['amount_cents']/100:+.2f}  "
              f"created={d['created_at']}  yid={d['ynab_txn_id']}")
        print(f"     payee = {d['payee']!r}")
