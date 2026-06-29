"""Collapse pending_txn pairs where the bot recorded the email-alert version
(`ynab_txn_id` like 'ledger:%') AND the YNAB-import version (real UUID) of the
same transaction.

Match: same (txn_date, amount_cents). Keep the UUID row (tied to real YNAB),
forward memo/raw_summary/suggested_category from the ledger row when the UUID
row is missing them, then mark the ledger row 'superseded'.
"""
from __future__ import annotations
import sqlite3

DB = "ynab_helper.db"

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row

rows = con.execute(
    "SELECT * FROM pending_txn WHERE status='pending'"
).fetchall()

by_key: dict[tuple, list[dict]] = {}
for r in rows:
    d = dict(r)
    key = (d["txn_date"], d["amount_cents"])
    by_key.setdefault(key, []).append(d)

merged = 0
for key, group in by_key.items():
    ledger = [r for r in group if (r["ynab_txn_id"] or "").startswith("ledger:")]
    real = [r for r in group if not (r["ynab_txn_id"] or "").startswith("ledger:")
            and r["ynab_txn_id"]]
    if not (ledger and real):
        continue
    # Take the first of each — if there are multiple UUIDs at the same
    # (date, amount), that's a different problem we don't try to solve here.
    keep = real[0]
    drop = ledger[0]

    updates = {}
    if not (keep["memo"] or "").strip() and (drop["memo"] or "").strip():
        updates["memo"] = drop["memo"]
    if keep["raw_summary"] is None and drop["raw_summary"]:
        updates["raw_summary"] = drop["raw_summary"]
    if keep["suggested_category"] is None and drop["suggested_category"]:
        updates["suggested_category"] = drop["suggested_category"]

    print(
        f"merge: keep #{keep['id']} ({keep['payee']}) <- drop #{drop['id']} "
        f"({drop['payee']}) | forwarding: {list(updates.keys()) or 'nothing'}"
    )

    if updates:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        con.execute(
            f"UPDATE pending_txn SET {set_clause} WHERE id = ?",
            (*updates.values(), keep["id"]),
        )
    con.execute(
        "UPDATE pending_txn SET status = 'skipped', "
        "memo = COALESCE(NULLIF(memo, ''), '') || "
        "  ' [superseded by pending_txn #' || ? || ']' "
        "WHERE id = ?",
        (keep["id"], drop["id"]),
    )
    merged += 1

con.commit()
con.close()
print(f"\nmerged {merged} pair{'s' if merged != 1 else ''}")
