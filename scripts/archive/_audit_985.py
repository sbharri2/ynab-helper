from bot import storage
with storage.connect("ynab_helper.db") as con:
    print("Audit around 2026-06-16 14:28 (when #985 was created):")
    rows = con.execute(
        "SELECT id, event, details, ts FROM audit_log "
        "WHERE ts BETWEEN '2026-06-16 14:25:00' AND '2026-06-16 14:35:00' "
        "ORDER BY id"
    ).fetchall()
    for r in rows:
        print(f"  #{r['id']} {str(r['ts'])[:19]} {r['event']:<28} {(r['details'] or '')[:100]}")

    print("\nFirst 5 categorize_via_override events ever:")
    rows = con.execute(
        "SELECT id, event, details, ts FROM audit_log "
        "WHERE event = 'categorize_via_override' ORDER BY id LIMIT 5"
    ).fetchall()
    for r in rows:
        print(f"  #{r['id']} {r['ts']}: {(r['details'] or '')[:100]}")

    print("\n#985 push events:")
    rows = con.execute(
        "SELECT id, event, details, ts FROM audit_log "
        "WHERE details LIKE '%985%' ORDER BY id DESC LIMIT 6"
    ).fetchall()
    for r in rows:
        print(f"  #{r['id']} {r['ts']} {r['event']}: {(r['details'] or '')[:100]}")
