"""Re-run categorization on all currently-pending pending_txn rows so the
new override + strongest-prior path can update bad LLM guesses.

Only re-suggests rows where the override or prior would produce a DIFFERENT
category than the current suggested_category. Doesn't touch user-confirmed
choices.
"""
from __future__ import annotations
from bot import storage
from bot.payee_overrides import resolve_payee_override

DB = "ynab_helper.db"

with storage.connect(DB) as con:
    rows = con.execute(
        """SELECT id, payee, amount_cents, txn_date, suggested_category
           FROM pending_txn
           WHERE status = 'pending'"""
    ).fetchall()
    rows = [dict(r) for r in rows]

print(f"Scanning {len(rows)} pending rows...\n")
updates = 0
for r in rows:
    payee = r["payee"] or ""
    new_cat = None
    src = None
    ov = resolve_payee_override(DB, payee)
    if ov:
        new_cat = ov["category_id"]
        src = f"override ({ov['rule']})"
    else:
        strong = storage.get_strongest_payee_category(DB, payee)
        if strong:
            new_cat = strong["category_id"]
            src = f"prior {strong['pct']*100:.0f}% ({strong['count']}/{strong['total_count']})"

    if new_cat and new_cat != r["suggested_category"]:
        with storage.connect(DB) as con:
            cn = con.execute(
                "SELECT name FROM category WHERE id = ?", (new_cat,)
            ).fetchone()
            cn = cn["name"] if cn else new_cat[:8]
            old = (r["suggested_category"] or "(none)")[:8]
            con.execute(
                "UPDATE pending_txn SET suggested_category = ? WHERE id = ?",
                (new_cat, r["id"]),
            )
        print(f"  #{r['id']} {r['txn_date']} ${r['amount_cents']/100:+.2f} "
              f"{payee[:25]:<25} {old}->{cn:<22}  via {src}")
        updates += 1

print(f"\nUpdated {updates} suggestions.")
storage.audit(DB, "resuggest_overrides_priors", {"updated": updates})
