"""Quick inspector for recent audit events + queue state."""
from __future__ import annotations
import json
from bot import storage

with storage.connect("ynab_helper.db") as con:
    print("Recent audit_log:")
    rows = con.execute(
        "SELECT id, event, details, ts FROM audit_log ORDER BY id DESC LIMIT 8"
    ).fetchall()
    for r in rows:
        try:
            d = json.loads(r["details"] or "{}")
        except Exception:
            d = {"raw": r["details"]}
        short = ", ".join(f"{k}={v}" for k, v in list(d.items())[:4])
        print(f"  #{r['id']:>5} {str(r['ts'])[:19]}  {r['event']:<28}  {short[:90]}")

    print("\nPending queue:")
    rows = con.execute(
        "SELECT status, SUM(CASE WHEN suggested_category IS NOT NULL THEN 1 ELSE 0 END) AS sug, COUNT(*) AS n "
        "FROM pending_txn GROUP BY status"
    ).fetchall()
    for r in rows:
        print(f"  {r['status']:<12} n={r['n']:<4} with_sug={r['sug']}")
