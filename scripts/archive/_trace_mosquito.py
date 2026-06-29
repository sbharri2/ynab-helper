from bot import storage
from bot.payee_overrides import resolve_payee_override

print("Override map test:")
r = resolve_payee_override("ynab_helper.db", "MOSQUITO JOE NC001 HOLLY SPRINGS USA")
print(f"  resolve_payee_override -> {r}")

print("\nStrongest historical prior test:")
r = storage.get_strongest_payee_category("ynab_helper.db", "MOSQUITO JOE NC001 HOLLY SPRINGS USA")
print(f"  get_strongest_payee_category -> {r}")
# Try without the location suffix
r = storage.get_strongest_payee_category("ynab_helper.db", "MOSQUITO JOE")
print(f"  get_strongest_payee_category('MOSQUITO JOE') -> {r}")

with storage.connect("ynab_helper.db") as con:
    print("\nHistorical MOSQUITO ledger_txn:")
    rows = con.execute(
        """SELECT id, posted_date, payee, amount_cents, category_id
           FROM ledger_txn
           WHERE LOWER(payee) LIKE '%mosquito%'
           ORDER BY posted_date DESC LIMIT 10"""
    ).fetchall()
    for r in rows:
        d = dict(r)
        cat = "(none)"
        if d["category_id"]:
            cr = con.execute(
                "SELECT name FROM category WHERE id = ?", (d["category_id"],),
            ).fetchone()
            if cr:
                cat = cr["name"]
        print(f"  lt#{d['id']}  {d['posted_date']}  ${d['amount_cents']/100:+.2f}  "
              f"cat={cat}  payee={d['payee']!r}")

    print("\nThe pending_txn for this $62 Mosquito Joe:")
    rows = con.execute(
        """SELECT id, txn_date, payee, amount_cents, raw_summary, status,
                  suggested_category, ynab_txn_id, created_at
           FROM pending_txn
           WHERE LOWER(payee) LIKE '%mosquito%'
           ORDER BY id DESC LIMIT 5"""
    ).fetchall()
    for r in rows:
        d = dict(r)
        cat = "(none)"
        if d["suggested_category"]:
            cr = con.execute(
                "SELECT name FROM category WHERE id = ?", (d["suggested_category"],),
            ).fetchone()
            if cr:
                cat = cr["name"]
        print(f"\n  #{d['id']}  {d['txn_date']}  ${d['amount_cents']/100:+.2f}  "
              f"status={d['status']}  created={d['created_at']}")
        print(f"     suggested = {cat}")
        print(f"     raw       = {d['raw_summary']!r}")

    print("\nMosquito Treatment category:")
    cat = con.execute(
        "SELECT id, name, is_spending FROM category WHERE LOWER(name) LIKE '%mosquito%'"
    ).fetchone()
    print(f"  {dict(cat) if cat else 'NOT FOUND'}")
