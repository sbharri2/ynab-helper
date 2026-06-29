"""Verify the new override + prior bypass for common bills."""
from __future__ import annotations
from bot import storage
from bot.payee_overrides import resolve_payee_override

DB = "ynab_helper.db"

TEST_PAYEES = [
    "AT&T",
    "AT&T MOBILITY",
    "ATT*BILL PAYMENT",
    "VERIZON WIRELESS",
    "T-MOBILE",
    "SPECTRUM SVC",
    "HULU.COM/BILL",
    "NETFLIX.COM",
    "AUDIBLE.COM",
    "AMAZON PRIME",
    "AGILEBITS 1PASSWORD",
    "YNAB",
    # Not in override map — should fall through
    "STARBUCKS",
    "LA TABERNA",
    "AMAZON MKTPLACE PMTS",
]

print("Override map test:")
for p in TEST_PAYEES:
    r = resolve_payee_override(DB, p)
    if r:
        print(f"  {p:<28} -> {r['category_name']}")
    else:
        print(f"  {p:<28} -> (no override)")

print("\nStrongest-prior test (looks at history):")
for p in ["AT&T", "AMAZON MKTPLACE PMTS", "VENMO", "HARRIS TEETER", "LA TABERNA"]:
    r = storage.get_strongest_payee_category(DB, p)
    if r:
        print(f"  {p:<28} -> {r['category_name']} "
              f"({r['count']}/{r['total_count']} = {r['pct']*100:.0f}%)")
    else:
        print(f"  {p:<28} -> (no strong prior)")

# Find any pending AT&T txns to show the immediate impact
print("\nPending AT&T pending_txns:")
with storage.connect(DB) as con:
    rows = con.execute(
        "SELECT id, payee, amount_cents, suggested_category "
        "FROM pending_txn "
        "WHERE status = 'pending' AND (payee LIKE '%AT&T%' OR payee LIKE '%ATT%')"
    ).fetchall()
    if not rows:
        print("  (none)")
    for r in rows:
        print(f"  #{r['id']} ${r['amount_cents']/100:.2f} {r['payee']!r}  "
              f"suggested={(r['suggested_category'] or '(none)')[:8]}")
