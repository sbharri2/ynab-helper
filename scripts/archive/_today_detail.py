from bot import storage
with storage.connect("ynab_helper.db") as con:
    rows = con.execute(
        "SELECT id, event, details, ts FROM audit_log "
        "WHERE event IN ('daily_summary_sent','daily_catchup','reconcile_mismatch') "
        "  AND ts >= '2026-06-15 00:00:00' "
        "ORDER BY id DESC"
    ).fetchall()
    for r in rows:
        print(f"#{r['id']} {r['ts']} {r['event']}")
        print(f"   {r['details']}")
