"""Apply the queue_lane migration and seed the cold lane.

Per the Phase 7 redesign, anything currently pending is moved to COLD
unless it was just created (last 4 hours). That way the bot doesn't
fire 50+ DMs the instant the new code lights up.
"""
from __future__ import annotations
from bot import storage

storage.init_db("ynab_helper.db")
print("migration applied")

with storage.connect("ynab_helper.db") as con:
    # Status of the queue right now
    before = con.execute(
        "SELECT queue_lane, COUNT(*) AS n "
        "FROM pending_txn WHERE status='pending' GROUP BY queue_lane"
    ).fetchall()
    print(f"before: {[dict(r) for r in before]}")

    # Send every pending row to COLD unless it was created in the last 4h.
    # Fresh items stay HOT so they still drip in real-time.
    rs = con.execute(
        "UPDATE pending_txn SET queue_lane = 'cold', "
        "lane_changed_at = CURRENT_TIMESTAMP "
        "WHERE status = 'pending' "
        "  AND queue_lane = 'hot' "
        "  AND created_at < datetime('now', '-4 hours')"
    )
    print(f"moved to COLD: {rs.rowcount} rows")

    after = con.execute(
        "SELECT queue_lane, COUNT(*) AS n "
        "FROM pending_txn WHERE status='pending' GROUP BY queue_lane"
    ).fetchall()
    print(f"after:  {[dict(r) for r in after]}")

storage.audit("ynab_helper.db", "phase7_cold_seed", {
    "moved_to_cold": rs.rowcount,
})
print("done")
