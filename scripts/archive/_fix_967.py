"""Enrich pending_txn #967 from the matching pending_order #26 (Rubber Golf
Tees) and re-suggest with the better summary now that the strong-prior
bypass skips Amazon-marketplace payees.
"""
from __future__ import annotations
import time
from bot import storage
from bot.config import load_settings
from bot.categorizer import Categorizer

settings = load_settings()
db = settings.paths.database

# 1. Enrich raw_summary from order #26
with storage.connect(db) as con:
    order = con.execute(
        "SELECT raw_summary, external_id FROM pending_order WHERE id = 26"
    ).fetchone()
    if order:
        new_summary = f"Amazon order {order['external_id']}: {order['raw_summary']}"
        con.execute(
            "UPDATE pending_txn SET raw_summary = ? WHERE id = 967",
            (new_summary,),
        )
        print(f"raw_summary <- {new_summary!r}")

# 2. Re-suggest with the enriched summary
engine = Categorizer(settings.ollama.endpoint, settings.ollama.model,
                     settings.ollama.temperature)
spending = storage.list_categories_for_spending(db)
cats = [{"id": c["id"], "name": c["name"], "group": c["group_name"]}
        for c in spending]

with storage.connect(db) as con:
    r = dict(con.execute(
        "SELECT id, payee, amount_cents, txn_date, raw_summary "
        "FROM pending_txn WHERE id = 967"
    ).fetchone())

priors = storage.get_category_priors_for_payee(db, r["payee"], top_n=5)
t0 = time.monotonic()
result = engine.suggest(
    summary=r["raw_summary"],
    amount_cents=r["amount_cents"],
    date_str=str(r["txn_date"]),
    source="citi_alert",
    categories=cats,
    priors=priors,
)
elapsed = int((time.monotonic() - t0) * 1000)
cid = result.get("category_id")
conf = float(result.get("confidence", 0) or 0)
cname = next((c["name"] for c in cats if c["id"] == cid), "(none)")
print(f"\nsuggest -> {cname} conf={conf:.2f} ({elapsed}ms)")

with storage.connect(db) as con:
    con.execute(
        "UPDATE pending_txn SET suggested_category = ? WHERE id = 967",
        (cid,),
    )
storage.audit(db, "manual_resuggest", {"pt_id": 967, "category": cid})
print("done")
