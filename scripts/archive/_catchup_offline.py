"""Offline catch-up: refresh NULL-suggestion pending_txn rows using payee +
memo only. Skips the Gmail scrape entirely so it works even when the OAuth
token used by daily_catchup.py is expired (the bot's email parsing has moved
to IMAP, but daily_catchup hasn't been migrated yet).
"""
from __future__ import annotations
import re
import time

from bot import storage
from bot.categorizer import Categorizer
from bot.config import load_settings

_GENERIC_PAYEE_RE = re.compile(r"^\s*(amazon|amzn|venmo|paypal)\b", re.I)

settings = load_settings()
db = settings.paths.database
cat_engine = Categorizer(
    settings.ollama.endpoint, settings.ollama.model, settings.ollama.temperature,
)

spending_cats = storage.list_categories_for_spending(db)
categories = [
    {"id": c["id"], "name": c["name"], "group": c["group_name"]}
    for c in spending_cats
]
print(f"loaded {len(categories)} spending categories")

with storage.connect(db) as con:
    rows = con.execute(
        """SELECT * FROM pending_txn
           WHERE status='pending' AND suggested_category IS NULL
           ORDER BY txn_date ASC, id ASC"""
    ).fetchall()
rows = [dict(r) for r in rows]
print(f"{len(rows)} rows need suggestions")

for row in rows:
    payee = row["payee"] or ""
    memo = (row["memo"] or "").strip()

    # Generic payees (Amazon/Venmo/PayPal) need email context we can't get
    # offline — defer until the IMAP catchup is wired.
    if _GENERIC_PAYEE_RE.match(payee):
        print(f"  defer (generic, no email): #{row['id']} {payee[:30]} ${row['amount_cents']/100:.2f}")
        continue

    summary = memo or payee
    priors = storage.get_category_priors_for_payee(db, payee, top_n=5)
    t0 = time.monotonic()
    result = cat_engine.suggest(
        summary=summary,
        amount_cents=row["amount_cents"],
        date_str=str(row["txn_date"]),
        source="ynab",
        categories=categories,
        priors=priors,
    )
    cat_id = result.get("category_id")
    cat_name = next((c["name"] for c in categories if c["id"] == cat_id), "(none)")
    conf = float(result.get("confidence", 0) or 0)
    dt_ms = int((time.monotonic() - t0) * 1000)
    print(
        f"  #{row['id']:>4} {row['txn_date']} ${row['amount_cents']/100:>10,.2f}  "
        f"{payee[:25]:<25} -> {cat_name[:25]:<25} (conf={conf:.2f}, {dt_ms}ms)"
    )
    if cat_id and conf >= 0.5:
        with storage.connect(db) as con:
            con.execute(
                "UPDATE pending_txn SET suggested_category=? WHERE id=?",
                (cat_id, row["id"]),
            )

storage.audit(db, "catchup_offline", {"rows_scanned": len(rows)})
print("done")
