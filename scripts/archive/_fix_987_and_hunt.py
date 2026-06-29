"""(1) Set #987's suggested_category to Mosquito Treatment (16th).
(2) Sweep all pending_txns created since 6/01 with NULL suggested_category
    whose audit log proves a categorize_via_* fired during their ingest.
    Backfill the missing suggestions.
"""
from __future__ import annotations
import json
from bot import storage

MOSQUITO_TREATMENT = "d87d4169-fc3f-44bd-8159-dee4e313b7e3"

with storage.connect("ynab_helper.db") as con:
    # (1) #987
    con.execute(
        "UPDATE pending_txn SET suggested_category = ? WHERE id = 987 AND suggested_category IS NULL",
        (MOSQUITO_TREATMENT,),
    )
    r = con.execute(
        "SELECT id, suggested_category FROM pending_txn WHERE id = 987"
    ).fetchone()
    print(f"#987 -> suggested_category = {r['suggested_category']}")

    # (2) Sweep: find pending_txn with NULL suggestion AND a categorize_via_*
    # audit event within ±5 seconds of its created_at.
    cands = con.execute(
        """SELECT id, payee, amount_cents, created_at FROM pending_txn
           WHERE suggested_category IS NULL
             AND created_at >= '2026-06-01'
             AND status = 'pending'
           ORDER BY id"""
    ).fetchall()
    print(f"\nScanning {len(cands)} NULL-suggestion candidates...")
    fixed = 0
    for c in cands:
        d = dict(c)
        audit_rows = con.execute(
            "SELECT details FROM audit_log "
            "WHERE event LIKE 'categorize_via_%' "
            "  AND ts BETWEEN datetime(?, '-10 seconds') AND datetime(?, '+10 seconds') "
            "  AND details LIKE ? "
            "LIMIT 1",
            (d["created_at"], d["created_at"], f"%{d['payee'][:30]}%"),
        ).fetchall()
        if not audit_rows:
            continue
        details = json.loads(audit_rows[0]["details"])
        cat_id = details.get("category_id")
        cat_name = details.get("category_name", "(?)")
        if not cat_id:
            continue
        con.execute(
            "UPDATE pending_txn SET suggested_category = ? WHERE id = ?",
            (cat_id, d["id"]),
        )
        print(f"  #{d['id']:>4} {d['payee'][:40]:<40} -> {cat_name}")
        fixed += 1
    print(f"\nBackfilled {fixed} suggestions.")

storage.audit("ynab_helper.db", "backfill_orphan_suggestions", {"count": fixed + 1})
print("done")
