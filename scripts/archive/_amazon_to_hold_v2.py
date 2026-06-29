from datetime import datetime, timezone
from bot import storage

ids_moved: list[int] = []
with storage.connect("ynab_helper.db") as con:
    rows = con.execute(
        "SELECT id, payee, amount_cents, txn_date, raw_summary "
        "FROM pending_txn "
        "WHERE status = 'pending' "
        "  AND queue_lane = 'cold' "
        "  AND (UPPER(payee) LIKE '%AMAZON%' OR UPPER(payee) LIKE '%AMZN%')"
    ).fetchall()
    for r in rows:
        raw = (r["raw_summary"] or "").strip()
        if raw.lower().startswith("amazon order"):
            continue
        ids_moved.append(r["id"])
    if ids_moved:
        placeholders = ",".join(["?"] * len(ids_moved))
        con.execute(
            f"UPDATE pending_txn SET queue_lane='hold', "
            f"lane_changed_at=? WHERE id IN ({placeholders})",
            [datetime.now(timezone.utc), *ids_moved],
        )

# Audit AFTER the with block so the connection is closed and committed
storage.audit("ynab_helper.db", "amazon_demoted_to_hold",
              {"count": len(ids_moved), "pt_ids": ids_moved})

print(f"Moved {len(ids_moved)} rows back to HOLD: {ids_moved}")

# Verify
with storage.connect("ynab_helper.db") as con:
    for pid in ids_moved:
        r = con.execute(
            "SELECT id, queue_lane, payee FROM pending_txn WHERE id=?", (pid,),
        ).fetchone()
        print(f"  pt#{r['id']}  lane={r['queue_lane']}  payee={r['payee']!r}")
