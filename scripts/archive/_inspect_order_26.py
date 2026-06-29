from bot import storage

with storage.connect("ynab_helper.db") as con:
    print("pending_order #26:")
    r = con.execute("SELECT * FROM pending_order WHERE id = 26").fetchone()
    for k, v in dict(r).items():
        print(f"  {k:<22} = {v}")

    print("\nAudit events touching pending_order id=26:")
    rows = con.execute(
        "SELECT id, event, details, ts FROM audit_log "
        "WHERE details LIKE '%\"id\": 26%' AND details LIKE '%pending_order%' "
        "ORDER BY id DESC LIMIT 8"
    ).fetchall()
    for r in rows:
        print(f"  #{r['id']:>5} {str(r['ts'])[:19]}  {r['event']:<28}  {r['details'][:120]}")

    print("\nLatest 8 audit events of any kind:")
    rows = con.execute(
        "SELECT id, event, details, ts FROM audit_log ORDER BY id DESC LIMIT 8"
    ).fetchall()
    for r in rows:
        print(f"  #{r['id']:>5} {str(r['ts'])[:19]}  {r['event']:<28}  {(r['details'] or '')[:90]}")
