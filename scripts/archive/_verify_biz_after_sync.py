"""Confirm Business Checking now reconciles against bank balance."""
from datetime import date
from bot import storage
from bot.reconciler import reconcile_account
from bot.envelope import recompute_month

YESTERDAY = date(2026, 6, 16)

with storage.connect("ynab_helper.db") as con:
    biz = con.execute(
        "SELECT id, name, balance_cents, cleared_balance_cents "
        "FROM account WHERE LOWER(name) LIKE '%9649%' OR LOWER(name) LIKE '%business%checking%'"
    ).fetchall()
    for a in biz:
        d = dict(a)
        print(f"Account {d['name']} ({d['id'][:8]}):")
        print(f"  balance_cents          = ${(d['balance_cents'] or 0)/100:,.2f}")
        print(f"  cleared_balance_cents  = ${(d['cleared_balance_cents'] or 0)/100:,.2f}")
        sum_row = con.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS s, COUNT(*) AS n "
            "FROM ledger_txn WHERE account_id = ?", (d["id"],),
        ).fetchone()
        print(f"  ledger_txn sum         = ${sum_row['s']/100:,.2f} ({sum_row['n']} rows)")

# Now actually run reconcile_account for yesterday
print("\nReconciling yesterday:")
with storage.connect("ynab_helper.db") as con:
    biz_ids = [r["id"] for r in con.execute(
        "SELECT id FROM account WHERE LOWER(name) LIKE '%9649%' OR LOWER(name) LIKE '%business%checking%'"
    ).fetchall()]
for aid in biz_ids:
    result = reconcile_account("ynab_helper.db", aid, YESTERDAY)
    print(f"  account {aid[:8]}: status={result['status']} "
          f"expected=${(result['expected_cents'] or 0)/100:,.2f}  "
          f"observed=${(result['observed_cents'] or 0)/100:,.2f}  "
          f"delta=${(result['delta_cents'] or 0)/100:+.2f}")

# Re-run all reconciles for yesterday to see fresh deltas overall
print("\nAll reconciles for yesterday:")
from bot.reconciler import reconcile_all_observed
results = reconcile_all_observed("ynab_helper.db", YESTERDAY)
for r in results:
    aid = r["account_id"]
    with storage.connect("ynab_helper.db") as con:
        nm = con.execute(
            "SELECT name FROM account WHERE id = ?", (aid,)
        ).fetchone()["name"]
    print(f"  {nm[:30]:<30}  {r['status']:<10}  delta=${(r['delta_cents'] or 0)/100:+,.2f}")

recompute_month("ynab_helper.db", "2026-06")
print("\nrecompute done")
