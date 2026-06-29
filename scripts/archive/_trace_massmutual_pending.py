from bot import storage
from bot.payee_overrides import resolve_payee_override

print("Test the override map directly:")
r = resolve_payee_override("ynab_helper.db", "MASSMUTUAL LIFE")
print(f"  resolve_payee_override('MASSMUTUAL LIFE') = {r}")

with storage.connect("ynab_helper.db") as con:
    print("\nAll MASSMUTUAL pending_txn rows:")
    rows = con.execute(
        """SELECT id, txn_date, payee, amount_cents, raw_summary, status,
                  suggested_category, ynab_txn_id, created_at, last_pushed_at
           FROM pending_txn
           WHERE payee LIKE '%MASSMUTUAL%' OR payee LIKE '%MASS MUTUAL%' OR payee LIKE '%MASSACHUSETTS%'
           ORDER BY id DESC LIMIT 10"""
    ).fetchall()
    for r in rows:
        d = dict(r)
        cat_name = "(none)"
        if d["suggested_category"]:
            cr = con.execute(
                "SELECT name FROM category WHERE id = ?", (d["suggested_category"],)
            ).fetchone()
            if cr:
                cat_name = cr["name"]
        print(f"\n  #{d['id']}  {d['txn_date']}  ${d['amount_cents']/100:+.2f}  "
              f"status={d['status']}  assignee=N/A")
        print(f"     payee     = {d['payee']!r}")
        print(f"     ynab_id   = {d['ynab_txn_id']}")
        print(f"     created   = {d['created_at']}")
        print(f"     pushed    = {d['last_pushed_at']}")
        print(f"     suggested = {cat_name}")

    print("\nMass Mutual Insurances category lookup:")
    cat = con.execute(
        "SELECT id, name, hidden, is_spending FROM category WHERE LOWER(name) LIKE '%mass mutual%'"
    ).fetchone()
    print(f"  {dict(cat) if cat else 'NOT FOUND'}")
