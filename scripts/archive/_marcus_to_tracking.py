"""Flip Marcus / Rainy Day Savings to a tracking account in the local ledger DB.

Why: Steven is converting the account in YNAB from Savings (on-budget) to a
Tracking account so the $32,571 stops sitting in Ready-to-Assign and isn't
covered by spending envelopes. Mirror the change locally so the daily
reporter and reconciler treat it as off-budget too.
"""
from __future__ import annotations

from bot import storage

MARCUS_ID = "cba85d5e-2ac2-4555-8a56-e9524d08df99"

with storage.connect("ynab_helper.db") as con:
    row = con.execute(
        "SELECT id, name, type, on_budget, closed FROM account WHERE id=?",
        (MARCUS_ID,),
    ).fetchone()
    if row is None:
        print(f"no account row for id={MARCUS_ID}")
        raise SystemExit(1)
    print("before:", dict(row))

    con.execute(
        "UPDATE account SET type='tracking', on_budget=0 WHERE id=?",
        (MARCUS_ID,),
    )

    row = con.execute(
        "SELECT id, name, type, on_budget, closed FROM account WHERE id=?",
        (MARCUS_ID,),
    ).fetchone()
    print("after: ", dict(row))

storage.audit(
    "ynab_helper.db",
    "account_type_change",
    {
        "account_id": MARCUS_ID,
        "name": "Rainy Day Savings",
        "from": {"type": "savings", "on_budget": 1},
        "to": {"type": "tracking", "on_budget": 0},
        "reason": "emergency/investment dry powder; not for envelope funding",
    },
)
print("audit logged")
