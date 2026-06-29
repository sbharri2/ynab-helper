from bot import storage
from bot.payee_overrides import resolve_payee_override

CASES = ["Amica Mutual Ins Lincoln USA", "AMICA MUTUAL INS", "Amica",
         "AMICA MUT INS"]
for p in CASES:
    ov = resolve_payee_override("ynab_helper.db", p)
    sp = storage.get_strongest_payee_category("ynab_helper.db", p)
    print(f"  {p!r}")
    print(f"    override -> {ov}")
    print(f"    prior    -> {sp}")

with storage.connect("ynab_helper.db") as con:
    print("\nHistorical AMICA ledger_txn rows:")
    rows = con.execute(
        "SELECT id, posted_date, amount_cents, payee, category_id "
        "FROM ledger_txn WHERE LOWER(payee) LIKE '%amica%' "
        "ORDER BY posted_date DESC LIMIT 15"
    ).fetchall()
    for r in rows:
        d = dict(r)
        cn = "(none)"
        if d["category_id"]:
            c = con.execute("SELECT name FROM category WHERE id = ?",
                             (d["category_id"],)).fetchone()
            if c:
                cn = c["name"]
        print(f"  lt#{d['id']:>5}  {d['posted_date']}  ${d['amount_cents']/100:>+10.2f}  "
              f"cat={cn[:30]:<30}  payee={d['payee']!r}")

    print("\nThe pending row for $404.90:")
    rows = con.execute(
        "SELECT id, txn_date, payee, amount_cents, raw_summary, queue_lane, "
        "       suggested_category, status, created_at "
        "FROM pending_txn WHERE LOWER(payee) LIKE '%amica%' "
        "ORDER BY id DESC LIMIT 5"
    ).fetchall()
    for r in rows:
        d = dict(r)
        cn = "(none)"
        if d["suggested_category"]:
            c = con.execute("SELECT name FROM category WHERE id = ?",
                             (d["suggested_category"],)).fetchone()
            if c:
                cn = c["name"]
        print(f"  pt#{d['id']}  {d['txn_date']}  ${d['amount_cents']/100:+.2f}  "
              f"lane={d['queue_lane']:<6}  status={d['status']}")
        print(f"     payee = {d['payee']!r}")
        print(f"     created = {d['created_at']}")
        print(f"     suggested = {cn}")

    print("\nAmica-related categories:")
    for r in con.execute(
        "SELECT id, name, is_spending FROM category WHERE LOWER(name) LIKE '%amica%'"
    ).fetchall():
        print(f"  {dict(r)}")
