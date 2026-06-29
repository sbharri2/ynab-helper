"""Move existing COLD-lane Amazon pending_txns BACK to HOLD when they
lack enrichment (no 'Amazon order' prefix on raw_summary).

These are items that landed before the new 14-day HOLD TTL kicked in,
and got demoted to COLD by the old 24h timer. Per Steven's directive,
they should stay in HOLD until the matching order email arrives or 14
days pass (which then surfaces them as an Amazon-tracker alert).
"""
from datetime import datetime, timezone
from bot import storage

with storage.connect("ynab_helper.db") as con:
    rows = con.execute(
        "SELECT id, payee, amount_cents, txn_date, raw_summary, queue_lane "
        "FROM pending_txn "
        "WHERE status = 'pending' "
        "  AND queue_lane = 'cold' "
        "  AND (UPPER(payee) LIKE '%AMAZON%' OR UPPER(payee) LIKE '%AMZN%')"
    ).fetchall()
    candidates = []
    for r in rows:
        raw = (r["raw_summary"] or "").strip()
        if raw.lower().startswith("amazon order"):
            # Already enriched — leave in COLD
            continue
        candidates.append(dict(r))

    print(f"Moving {len(candidates)} unenriched Amazon rows back to HOLD:")
    for c in candidates:
        print(f"  pt#{c['id']:>4}  {c['txn_date']}  ${c['amount_cents']/100:+.2f}  "
              f"payee={c['payee']!r}")
        print(f"     raw={(c['raw_summary'] or '')[:60]!r}")

    if candidates:
        ids = [c["id"] for c in candidates]
        placeholders = ",".join(["?"] * len(ids))
        con.execute(
            f"UPDATE pending_txn SET queue_lane = 'hold', "
            f"lane_changed_at = ? WHERE id IN ({placeholders})",
            [datetime.now(timezone.utc), *ids],
        )
        storage.audit("ynab_helper.db", "amazon_demoted_to_hold", {
            "count": len(ids), "pt_ids": ids,
        })
print("done")
