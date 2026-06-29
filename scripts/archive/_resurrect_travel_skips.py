"""Move recently skipped pending_txns back into the COLD lane so Steven
can test /batch against real (uncategorized) data."""
from bot import storage
from datetime import datetime, timezone, timedelta

CUTOFF = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

with storage.connect("ynab_helper.db") as con:
    skipped = con.execute(
        "SELECT id, payee, amount_cents, txn_date FROM pending_txn "
        "WHERE status='skipped' AND created_at >= ?",
        (CUTOFF,),
    ).fetchall()
    print(f"resurrecting {len(skipped)} skipped rows back to COLD")
    for r in skipped:
        d = dict(r)
        print(f"  pt#{d['id']:>4}  {d['txn_date']}  ${d['amount_cents']/100:+.2f}  "
              f"{d['payee'][:40]}")
    if skipped:
        ids = [r["id"] for r in skipped]
        placeholders = ",".join(["?"] * len(ids))
        con.execute(
            f"UPDATE pending_txn SET status='pending', queue_lane='cold', "
            f"lane_changed_at=CURRENT_TIMESTAMP "
            f"WHERE id IN ({placeholders})",
            ids,
        )
    print(f"done")
storage.audit("ynab_helper.db", "resurrect_travel_skips", {"count": len(skipped)})
