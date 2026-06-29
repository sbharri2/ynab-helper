"""Consolidate the three duplicate rows for the $38.20 Amazon charge.

Strategy: keep #977 as the canonical, re-enrich from order #26 (Rubber Golf
Tees), re-suggest with LLM. Mark #999 + ledger 23818 as superseded. #998
stays skipped.
"""
from __future__ import annotations
import time
from datetime import datetime, timezone

from bot import storage
from bot.config import load_settings
from bot.categorizer import Categorizer
from bot.envelope import recompute_month

settings = load_settings()
db = settings.paths.database

with storage.connect(db) as con:
    # Pull #26 (the matched Amazon order)
    o = con.execute(
        "SELECT raw_summary, external_id, chosen_category FROM pending_order WHERE id = 26"
    ).fetchone()
    if not o:
        print("order #26 missing — aborting")
        raise SystemExit(1)
    new_summary = f"Amazon order {o['external_id']}: {o['raw_summary']}"

    # 1. Re-apply enrichment to #977
    con.execute(
        "UPDATE pending_txn SET raw_summary = ?, last_pushed_at = NULL "
        "WHERE id = 977",
        (new_summary,),
    )
    print(f"#977 raw_summary -> {new_summary!r}")

    # 2. Skip #999 (the duplicate from re-ingested chase alert) and delete
    #    its orphan ledger_txn 23818 + clear the conversation pointer if it
    #    happens to be on either dupe.
    con.execute(
        "UPDATE pending_txn SET status='skipped', "
        "memo = COALESCE(NULLIF(memo, ''), '') || "
        "  ' [duplicate of pending_txn #977; gmail_watcher re-poll bypassed dedupe]' "
        "WHERE id = 999 AND status='pending'"
    )
    con.execute("DELETE FROM ledger_signal WHERE ledger_txn_id = 23818")
    con.execute("DELETE FROM ledger_txn WHERE id = 23818")
    print("#999 marked skipped; lt#23818 deleted")

# 3. Re-suggest with the enriched summary
engine = Categorizer(settings.ollama.endpoint, settings.ollama.model,
                     settings.ollama.temperature)
spending = storage.list_categories_for_spending(db)
cats = [{"id": c["id"], "name": c["name"], "group": c["group_name"]}
        for c in spending]
with storage.connect(db) as con:
    r = dict(con.execute(
        "SELECT id, payee, amount_cents, txn_date, raw_summary FROM pending_txn "
        "WHERE id = 977"
    ).fetchone())
priors = storage.get_category_priors_for_payee(db, r["payee"], top_n=5)
t0 = time.monotonic()
result = engine.suggest(
    summary=r["raw_summary"], amount_cents=r["amount_cents"],
    date_str=str(r["txn_date"]), source="citi_alert",
    categories=cats, priors=priors,
)
cid = result.get("category_id")
cname = next((c["name"] for c in cats if c["id"] == cid), "(none)")
print(f"re-suggest -> {cname} ({int((time.monotonic()-t0)*1000)}ms)")
if cid:
    with storage.connect(db) as con:
        con.execute(
            "UPDATE pending_txn SET suggested_category = ? WHERE id = 977",
            (cid,),
        )

# Recompute the month
recompute_month(db, "2026-06")
storage.audit(db, "consolidate_3820_dupes", {
    "kept": 977, "skipped": [998, 999],
    "deleted_ledger": [23818], "new_suggestion": cid,
})
print("done")
