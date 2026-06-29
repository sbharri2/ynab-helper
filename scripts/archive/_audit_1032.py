from bot import storage
with storage.connect("ynab_helper.db") as con:
    print("Audit around 2026-06-26 09:42 (when #1032 was created):")
    rows = con.execute(
        "SELECT id, event, details, ts FROM audit_log "
        "WHERE ts BETWEEN '2026-06-26 09:42:00' AND '2026-06-26 09:48:00' "
        "ORDER BY id"
    ).fetchall()
    for r in rows:
        print(f"  #{r['id']} {str(r['ts'])[:19]} {r['event']:<30} {(r['details'] or '')[:130]}")
