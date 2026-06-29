"""Surface which pending_txns went through the LLM path (no override/prior
audit event near their creation) and what the LLM picked. Lets us judge
the LLM's actual hit rate on stuff the rules-engine couldn't catch.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone, timedelta
from bot import storage

DB = "ynab_helper.db"
CUTOFF = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

with storage.connect(DB) as con:
    cat_name = {r["id"]: r["name"]
                for r in con.execute("SELECT id, name FROM category").fetchall()}

    # Pull every pending_txn created in window with a suggestion
    rows = con.execute(
        "SELECT id, txn_date, payee, amount_cents, raw_summary, suggested_category, "
        "status, chosen_category, created_at "
        "FROM pending_txn "
        "WHERE created_at >= ? AND suggested_category IS NOT NULL "
        "ORDER BY id",
        (CUTOFF,),
    ).fetchall()

    deterministic_pt_ids = set()
    # For each row, look for a categorize_via_* event near its created_at.
    for r in rows:
        ev = con.execute(
            "SELECT id FROM audit_log "
            "WHERE event LIKE 'categorize_via_%' "
            "  AND ts BETWEEN datetime(?, '-15 seconds') AND datetime(?, '+15 seconds') "
            "  AND details LIKE ? "
            "LIMIT 1",
            (r["created_at"], r["created_at"], f"%{r['payee'][:25]}%"),
        ).fetchone()
        if ev:
            deterministic_pt_ids.add(r["id"])

    llm_rows = [r for r in rows if r["id"] not in deterministic_pt_ids]
    print(f"Rows attributable to LLM path: {len(llm_rows)}")
    print()
    print(f"{'pt_id':<6} {'date':<12} {'amount':>10}  {'payee':<32}  "
          f"{'LLM picked':<28}  {'user picked':<20}  status")
    print("-" * 130)
    for r in llm_rows:
        amt = r["amount_cents"] / 100
        sug = cat_name.get(r["suggested_category"], "(?)")[:28]
        ch = cat_name.get(r["chosen_category"], "") if r["chosen_category"] else "—"
        ch = ch[:20] if ch != "—" else ch
        verdict = ""
        if r["status"] == "categorized" and r["chosen_category"] == r["suggested_category"]:
            verdict = "CONFIRMED ✓"
        elif r["status"] == "categorized" and r["chosen_category"]:
            verdict = "USER OVERRODE"
        elif r["status"] == "skipped":
            verdict = "skipped"
        else:
            verdict = r["status"]
        print(f"{r['id']:<6} {str(r['txn_date'])[:10]:<12} ${amt:>+8.2f}  "
              f"{(r['payee'] or '')[:32]:<32}  {sug:<28}  {ch:<20}  {verdict}")
