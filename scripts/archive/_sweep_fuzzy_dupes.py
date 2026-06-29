"""Find and consolidate pending_txn pairs that are clearly the same
real-world charge but differ in payee format and date by ±2 days.

Pair criteria:
  * Both status='pending', queue_lane='cold'
  * Same |amount_cents|
  * Same account (ynab_account_id)
  * Posted dates within ±2 days
  * One has ynab_txn_id starting 'ledger:' (CC-alert ingest)
  * Other has a real YNAB UUID (ynab_watcher pre-strip)

Keep the ledger:N row (it has richer raw_summary and ran through the
override/prior pipeline). Mark the other as skipped with a memo, and
adopt its real YNAB id onto the keeper.
"""
from __future__ import annotations
from collections import defaultdict
from bot import storage

with storage.connect("ynab_helper.db") as con:
    rows = con.execute(
        """SELECT id, payee, amount_cents, txn_date, ynab_txn_id,
                  ynab_account_id, suggested_category, raw_summary
           FROM pending_txn
           WHERE status = 'pending' AND queue_lane = 'cold'
           ORDER BY txn_date, ABS(amount_cents)"""
    ).fetchall()
    rows = [dict(r) for r in rows]

# Group by (account, |amount|)
by_key = defaultdict(list)
for r in rows:
    key = (r["ynab_account_id"], abs(r["amount_cents"]))
    by_key[key].append(r)

# Find pairs within ±2 days; one ledger:N + one real YNAB UUID
from datetime import date as _d
def to_date(v):
    if isinstance(v, _d):
        return v
    return _d.fromisoformat(str(v)[:10])

pairs_to_merge = []  # (keeper_id, drop_id, keeper_old_yid, drop_real_yid)
for key, group in by_key.items():
    if len(group) < 2:
        continue
    for i, a in enumerate(group):
        for b in group[i+1:]:
            d1 = to_date(a["txn_date"])
            d2 = to_date(b["txn_date"])
            if abs((d1 - d2).days) > 2:
                continue
            a_is_ledger = (a["ynab_txn_id"] or "").startswith("ledger:")
            b_is_ledger = (b["ynab_txn_id"] or "").startswith("ledger:")
            if a_is_ledger and not b_is_ledger:
                keeper, drop = a, b
            elif b_is_ledger and not a_is_ledger:
                keeper, drop = b, a
            else:
                continue
            pairs_to_merge.append({
                "keeper_id": keeper["id"],
                "keeper_payee": keeper["payee"],
                "keeper_old_yid": keeper["ynab_txn_id"],
                "drop_id": drop["id"],
                "drop_payee": drop["payee"],
                "drop_yid": drop["ynab_txn_id"],
                "amount": a["amount_cents"],
                "date_gap": abs((d1 - d2).days),
            })

print(f"Found {len(pairs_to_merge)} fuzzy-dupe pair(s):\n")
for p in pairs_to_merge:
    print(f"  ${p['amount']/100:+.2f}  Δdays={p['date_gap']}")
    print(f"    KEEP   pt#{p['keeper_id']:>4}  {p['keeper_payee']!r}")
    print(f"    DROP   pt#{p['drop_id']:>4}  {p['drop_payee']!r}")
    print()

if pairs_to_merge:
    with storage.connect("ynab_helper.db") as con:
        for p in pairs_to_merge:
            # Sentinel-then-promote pattern (UNIQUE on ynab_txn_id)
            sentinel = f"merged:{p['drop_id']}"
            con.execute(
                "UPDATE pending_txn SET status='skipped', ynab_txn_id=?, "
                "memo = COALESCE(NULLIF(memo, ''), '') || "
                "  ' [fuzzy-merged into pt#' || ? || ']' "
                "WHERE id=?",
                (sentinel, p["keeper_id"], p["drop_id"]),
            )
            con.execute(
                "UPDATE pending_txn SET ynab_txn_id=? WHERE id=?",
                (p["drop_yid"], p["keeper_id"]),
            )
    storage.audit("ynab_helper.db", "fuzzy_dupe_sweep_2026_06_26", {
        "count": len(pairs_to_merge),
        "pairs": [(p["keeper_id"], p["drop_id"]) for p in pairs_to_merge],
    })
print("done")
