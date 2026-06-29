"""One-shot: pin Marcus / Rainy Day Savings observed balance to reality.

Run after Steven confirms the current Marcus balance via the bank's website;
the bot has no parser for Marcus yet, so the daily report falls back to the
stale YNAB import value otherwise.
"""
from datetime import date
from bot import storage

MARCUS_ID = "cba85d5e-2ac2-4555-8a56-e9524d08df99"  # Rainy Day Savings
BALANCE_CENTS = 3257129  # $32,571.29 — Steven confirmed 2026-06-09

rid = storage.record_observed_balance(
    "ynab_helper.db",
    account_id=MARCUS_ID,
    as_of_date=date.today(),
    balance_cents=BALANCE_CENTS,
    source_email_id=None,
)
if rid is None:
    print("Already filed for today — overwriting.")
    import sqlite3
    con = sqlite3.connect("ynab_helper.db")
    con.execute(
        "UPDATE account_balance_observed SET balance_cents=? WHERE account_id=? AND as_of_date=?",
        (BALANCE_CENTS, MARCUS_ID, date.today().isoformat()),
    )
    con.commit()
    con.close()
    print(f"Updated to ${BALANCE_CENTS/100:,.2f}")
else:
    print(f"Inserted row {rid}: Marcus = ${BALANCE_CENTS/100:,.2f}")
