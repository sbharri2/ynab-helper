from bot import storage
with storage.connect("ynab_helper.db") as con:
    print("pending_txn $15 Amazon on/near 6/14:")
    rows = con.execute(
        """SELECT id, txn_date, payee, amount_cents, raw_summary, status,
                  ynab_txn_id, suggested_category, last_pushed_at
           FROM pending_txn
           WHERE amount_cents BETWEEN -1600 AND -1400
             AND txn_date BETWEEN '2026-06-13' AND '2026-06-16'
             AND (payee LIKE '%AMAZON%' OR payee LIKE '%AMZN%')
           ORDER BY id"""
    ).fetchall()
    for r in rows:
        amt = (r["amount_cents"] or 0) / 100
        sug = (r["suggested_category"] or "(none)")[:8]
        print(f"  #{r['id']}  {r['txn_date']}  ${amt:+.2f}  status={r['status']}  sug={sug}")
        print(f"     payee     = {r['payee']!r}")
        print(f"     raw       = {(r['raw_summary'] or '')[:90]}")
        print(f"     pushed    = {r['last_pushed_at']}")
        print()

    print("pending_order $15 Amazon on/near 6/14:")
    rows = con.execute(
        """SELECT id, source, order_date, total_cents, status, raw_summary,
                  chosen_category, external_id
           FROM pending_order
           WHERE total_cents BETWEEN 1400 AND 1600
             AND order_date BETWEEN '2026-06-13' AND '2026-06-16'
             AND source = 'amazon'"""
    ).fetchall()
    for r in rows:
        amt = (r["total_cents"] or 0) / 100
        cat = (r["chosen_category"] or "(none)")[:8]
        print(f"  #{r['id']}  {r['order_date']}  ${amt:.2f}  status={r['status']}  cat={cat}")
        print(f"     raw      = {(r['raw_summary'] or '')[:80]}")
        print(f"     ext_id   = {r['external_id']}")

    print("\nAudit events for the categorization decision of this pending_txn:")
    rows = con.execute(
        "SELECT id, event, details, ts FROM audit_log "
        "WHERE event IN ('categorize_via_override','categorize_via_prior','ledger_ingest') "
        "  AND ts >= '2026-06-15 20:00:00' AND details LIKE '%AMAZON%' "
        "ORDER BY id DESC LIMIT 10"
    ).fetchall()
    for r in rows:
        print(f"  #{r['id']} {str(r['ts'])[:19]} {r['event']}: {(r['details'] or '')[:120]}")
