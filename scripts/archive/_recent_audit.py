from bot import storage
with storage.connect("ynab_helper.db") as con:
    print("All audit events in last 30 min:")
    rows = con.execute(
        "SELECT id, event, details, ts FROM audit_log "
        "WHERE ts >= datetime('now', '-30 minutes') "
        "ORDER BY id DESC LIMIT 40"
    ).fetchall()
    for r in rows:
        print(f"  #{r['id']} {str(r['ts'])[:19]} {r['event']:<28} {(r['details'] or '')[:80]}")

    print("\nCategory names for IDs from queue:")
    cids = ["0c3aa81c-6ab3-48a1-a63c-bdd7164079cd", "a9be1fd8", "f4b8c004", "1fbde6be"]
    for cid_prefix in cids:
        row = con.execute(
            "SELECT name FROM category WHERE id LIKE ?", (f"{cid_prefix}%",)
        ).fetchone()
        if row:
            print(f"  {cid_prefix[:8]} = {row['name']}")
