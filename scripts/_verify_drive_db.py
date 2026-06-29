"""Confirm bot is reading the Drive-located DB after the move."""
from bot.config import load_settings
from bot import storage

settings = load_settings()
print(f"settings.paths.database = {settings.paths.database}")
with storage.connect(settings.paths.database) as con:
    n = con.execute("SELECT COUNT(*) FROM ledger_txn").fetchone()[0]
    print(f"  ledger_txn rows: {n}")
    last = con.execute(
        "SELECT id, event, ts FROM audit_log ORDER BY id DESC LIMIT 3"
    ).fetchall()
    print("  last 3 audit events:")
    for r in last:
        print(f"    #{r['id']} {r['ts']} {r['event']}")
storage.audit(settings.paths.database, "drive_db_verify", {"ok": True})
print("audit OK")
