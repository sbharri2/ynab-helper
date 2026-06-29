"""Re-route pending_txn #972 (Smith Sinnett $2465 paycheck) from
'Allison Reimbursables' to 'Inflow: Ready to Assign'.
"""
from datetime import datetime, timezone
from bot import storage
from bot.envelope import recompute_month
from bot.ynab_client import YnabClient
from bot.config import load_settings

INFLOW_RTA = "1fbde6be-7144-4752-ae25-6afa80b09f95"
settings = load_settings()

with storage.connect("ynab_helper.db") as con:
    r = con.execute(
        "SELECT id, ynab_txn_id, chosen_category, status FROM pending_txn WHERE id = 972"
    ).fetchone()
    print(f"before: {dict(r)}")

    con.execute(
        "UPDATE pending_txn SET chosen_category = ?, "
        "chosen_at = ?, status = 'categorized' WHERE id = 972",
        (INFLOW_RTA, datetime.now(timezone.utc)),
    )

    # Update local ledger_txn if linked
    ynab_id = r["ynab_txn_id"]
    if ynab_id and ynab_id.startswith("ledger:"):
        lid = int(ynab_id.split(":", 1)[1])
        con.execute(
            "UPDATE ledger_txn SET category_id = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ?", (INFLOW_RTA, lid),
        )
        print(f"updated ledger_txn {lid}")
    elif ynab_id:
        con.execute(
            "UPDATE ledger_txn SET category_id = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE ynab_txn_id = ?", (INFLOW_RTA, ynab_id),
        )
        # Also push to YNAB
        try:
            yc = YnabClient(settings.ynab_token, settings.ynab.budget_id)
            yc.set_category(ynab_id, INFLOW_RTA)
            print(f"pushed YNAB set_category({ynab_id}) -> Inflow:RTA")
        except Exception as e:
            print(f"YNAB push failed: {e}")

    r = con.execute(
        "SELECT id, chosen_category FROM pending_txn WHERE id = 972"
    ).fetchone()
    print(f"after: {dict(r)}")

# Recompute affected categories
print("\nRecomputing 2026-06:")
recompute_month("ynab_helper.db", "2026-06")
storage.audit("ynab_helper.db", "fix_paycheck_972", {
    "from_category": "Allison Reimbursables",
    "to_category": "Inflow: Ready to Assign",
})
print("done")
