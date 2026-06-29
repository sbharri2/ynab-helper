"""Re-run categorizer on the two enriched Apple pending_txn rows.

Why: when the rows were first ingested, the bot only had the bare
'APPLE.COM/BILL' payee and produced the wrong guess ('Dining Out/
Entertainment'). Now that we've enriched raw_summary with the receipt detail
(NYT Wordle, Apple One), re-run with the better input to refresh the
'Best guess:' line.
"""
from __future__ import annotations
import time
from bot import storage
from bot.config import load_settings
from bot.categorizer import Categorizer

settings = load_settings()
db = settings.paths.database
engine = Categorizer(
    settings.ollama.endpoint, settings.ollama.model, settings.ollama.temperature,
)
spending = storage.list_categories_for_spending(db)
cats = [{"id": c["id"], "name": c["name"], "group": c["group_name"]}
        for c in spending]
print(f"using {len(cats)} spending categories")

TARGETS = [922, 923]

with storage.connect(db) as con:
    for tid in TARGETS:
        row = dict(con.execute(
            "SELECT id, payee, amount_cents, txn_date, raw_summary, suggested_category "
            "FROM pending_txn WHERE id = ?",
            (tid,),
        ).fetchone() or {})
        if not row:
            print(f"#{tid}: not found")
            continue
        summary = row["raw_summary"] or row["payee"] or ""
        priors = storage.get_category_priors_for_payee(db, row["payee"], top_n=5)
        t0 = time.monotonic()
        result = engine.suggest(
            summary=summary,
            amount_cents=row["amount_cents"],
            date_str=str(row["txn_date"]),
            source="citi_alert",
            categories=cats,
            priors=priors,
        )
        elapsed = int((time.monotonic() - t0) * 1000)
        cid = result.get("category_id")
        conf = float(result.get("confidence", 0) or 0)
        cname = next((c["name"] for c in cats if c["id"] == cid), None) or "(none)"
        print(f"#{tid}: ${row['amount_cents']/100:.2f}  {summary[:60]}")
        print(f"   suggest -> {cname:<35} conf={conf:.2f}  ({elapsed}ms)")
        if cid and conf >= 0.5:
            con.execute(
                "UPDATE pending_txn SET suggested_category = ? WHERE id = ?",
                (cid, tid),
            )
            print(f"   updated suggested_category = {cid}")
        else:
            print(f"   left as-is (confidence too low or no id)")

storage.audit(db, "apple_resuggest", {"target_ids": TARGETS})
