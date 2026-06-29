"""Repair pending_txn #985 — the original MASSMUTUAL row that got
left behind by the 6/16 parser-sign bugfix.

Sets suggested_category to Mass Mutual Insurances (17th) and flips
amount_cents from +$110.01 to -$110.01 so it matches the corrected
ledger_txn 23655.
"""
from bot import storage

MASS_MUTUAL_INSURANCES = "9a2ea642-20fa-4bb5-9da5-280ad750ace0"

with storage.connect("ynab_helper.db") as con:
    r = con.execute(
        "SELECT amount_cents, suggested_category, status FROM pending_txn WHERE id = 985"
    ).fetchone()
    print(f"before: {dict(r)}")
    con.execute(
        "UPDATE pending_txn SET "
        "  amount_cents = -ABS(amount_cents), "
        "  suggested_category = ? "
        "WHERE id = 985",
        (MASS_MUTUAL_INSURANCES,),
    )
    r = con.execute(
        "SELECT amount_cents, suggested_category, status FROM pending_txn WHERE id = 985"
    ).fetchone()
    print(f"after:  {dict(r)}")

storage.audit("ynab_helper.db", "fix_985_orphan", {
    "category": "Mass Mutual Insurances (17th)",
    "amount_sign": "flipped + to -",
})
print("done")
