from bot import storage
from bot.payee_overrides import resolve_payee_override

PAYEE = "HOLLYSPRINGS*UTILITIES HOLLY SPRINGS USA"
print(f"Override map: {resolve_payee_override('ynab_helper.db', PAYEE)}")
print(f"Strong prior: {storage.get_strongest_payee_category('ynab_helper.db', PAYEE)}")
# Try alternate spellings
for variant in ("HOLLYSPRINGS*UTILITIES", "HOLLY SPRINGS UTILITIES",
                "TOWN OF HOLLY SPRINGS", "HOLLYSPRINGS"):
    print(f"\nVariant: {variant!r}")
    print(f"  Override -> {resolve_payee_override('ynab_helper.db', variant)}")
    print(f"  Prior    -> {storage.get_strongest_payee_category('ynab_helper.db', variant)}")

with storage.connect("ynab_helper.db") as con:
    print("\nHistorical ledger_txn matching any Holly Springs utility:")
    rows = con.execute(
        """SELECT id, posted_date, amount_cents, payee, category_id
           FROM ledger_txn
           WHERE LOWER(payee) LIKE '%hollysprings%' OR LOWER(payee) LIKE '%holly springs%'
              OR LOWER(payee) LIKE '%town of holly%' OR LOWER(payee) LIKE '%utilities%'
           ORDER BY posted_date DESC LIMIT 25"""
    ).fetchall()
    for r in rows:
        d = dict(r)
        cn = "(none)"
        if d["category_id"]:
            cr = con.execute(
                "SELECT name FROM category WHERE id = ?", (d["category_id"],),
            ).fetchone()
            if cr:
                cn = cr["name"]
        print(f"  lt#{d['id']:>5}  {d['posted_date']}  ${d['amount_cents']/100:>+9.2f}  "
              f"cat={cn[:30]:<30}  payee={d['payee']!r}")
    print("\nAll utility-like categories:")
    for r in con.execute(
        "SELECT id, name, is_spending FROM category "
        "WHERE LOWER(name) LIKE '%water%' OR LOWER(name) LIKE '%electric%' "
        "  OR LOWER(name) LIKE '%trash%' OR LOWER(name) LIKE '%utility%'"
    ).fetchall():
        print(f"  {dict(r)}")
