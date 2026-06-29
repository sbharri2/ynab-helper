from bot import storage
with storage.connect("ynab_helper.db") as con:
    rows = con.execute(
        "SELECT id, event, ts FROM audit_log ORDER BY id DESC LIMIT 8"
    ).fetchall()
    for r in rows:
        print(f"  #{r['id']} {str(r['ts'])[:19]} {r['event']}")
