from bot import storage
with storage.connect("ynab_helper.db") as con:
    print("pending_txn #974:")
    r = con.execute(
        "SELECT id, txn_date, payee, amount_cents, raw_summary, "
        "suggested_category, status, created_at, last_pushed_at, ynab_txn_id "
        "FROM pending_txn WHERE id = 974"
    ).fetchone()
    print(f"  {dict(r)}")

    print("\nMatching pending_order #28 (Amazon $10.71 6/15):")
    r2 = con.execute(
        "SELECT id, source, order_date, total_cents, status, "
        "raw_summary, chosen_category, chosen_at, created_at, external_id "
        "FROM pending_order WHERE id = 28"
    ).fetchone()
    print(f"  {dict(r2)}")

    print("\nledger_txn linked to #974:")
    lr = con.execute(
        "SELECT id, posted_date, amount_cents, payee, category_id, "
        "source_signal, source_email_id, created_at "
        "FROM ledger_txn WHERE id = (SELECT CAST(SUBSTR(ynab_txn_id, 8) AS INTEGER) "
        "                            FROM pending_txn WHERE id = 974)"
    ).fetchone()
    if lr:
        print(f"  {dict(lr)}")

    print("\nAudit events around #974's creation:")
    if r:
        rows = con.execute(
            "SELECT id, event, details, ts FROM audit_log "
            "WHERE ts BETWEEN datetime(?, '-2 minutes') AND datetime(?, '+2 minutes') "
            "  AND (event LIKE 'ingest%' OR event LIKE 'ledger_ingest' "
            "       OR event LIKE 'categorize_via%') "
            "ORDER BY id",
            (r["created_at"], r["created_at"]),
        ).fetchall()
        for ar in rows:
            print(f"  #{ar['id']} {str(ar['ts'])[:19]} {ar['event']}: {(ar['details'] or '')[:120]}")

    print("\nAll 'ingest_enriched_from_order' events ever:")
    rows = con.execute(
        "SELECT id, event, details, ts FROM audit_log "
        "WHERE event = 'ingest_enriched_from_order' ORDER BY id DESC LIMIT 5"
    ).fetchall()
    for ar in rows:
        print(f"  #{ar['id']} {ar['ts']}: {ar['details']}")
    if not rows:
        print("  (none — Phase 2 enrichment has NEVER fired)")
