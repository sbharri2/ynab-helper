"""Inspect the txns Steven just categorized to find the budget-left bug."""
from bot import storage

with storage.connect("ynab_helper.db") as con:
    print("Recent pending_txns (921-929):")
    rows = con.execute(
        "SELECT id, ynab_txn_id, payee, amount_cents, chosen_category, raw_summary "
        "FROM pending_txn WHERE id BETWEEN 921 AND 929 ORDER BY id"
    ).fetchall()
    for r in rows:
        is_ledger = (r["ynab_txn_id"] or "").startswith("ledger:")
        marker = "LEDGER" if is_ledger else "YNAB  "
        amt = (r["amount_cents"] or 0) / 100
        print(f"  #{r['id']}  {marker}  ${amt:>+8.2f}  ynab_id={r['ynab_txn_id']}  cat={r['chosen_category'][:8] if r['chosen_category'] else 'None':<8}  {(r['payee'] or '')[:30]}")

    print("\nLedger_txn rows linked from these (by ledger:N):")
    rows = con.execute(
        "SELECT pt.id AS pt_id, pt.ynab_txn_id, lt.id AS lt_id, lt.category_id, lt.posted_date, lt.amount_cents "
        "FROM pending_txn pt "
        "LEFT JOIN ledger_txn lt ON lt.id = CAST(SUBSTR(pt.ynab_txn_id, 8) AS INTEGER) "
        "WHERE pt.id BETWEEN 921 AND 929 AND pt.ynab_txn_id LIKE 'ledger:%'"
    ).fetchall()
    for r in rows:
        amt = (r["amount_cents"] or 0) / 100
        cat = (r["category_id"] or "(none)")[:8]
        print(f"  pt#{r['pt_id']}  -> lt#{r['lt_id']}  cat={cat}  {r['posted_date']}  ${amt:+.2f}")

    print("\nCategory available balances for categories just used:")
    used = {
        "f4b8c004-92d9-4aa4-bef9-96f097fe2586",
        "f1033dd3-4fc4-410e-b75f-0fa6b902d624",
        "05049099-1e98-42cd-8b29-0d54b7e59b72",
        "e593c118-3b44-4834-be57-a62fb8a1a09b",
        "b5527483-247d-4997-8f0d-55cba9d58794",
    }
    for cid in used:
        row = con.execute(
            "SELECT c.name, mc.budgeted_cents, mc.activity_cents, mc.available_cents "
            "FROM category c LEFT JOIN month_category mc ON mc.category_id = c.id AND mc.month = '2026-06' "
            "WHERE c.id = ?",
            (cid,),
        ).fetchone()
        if not row:
            continue
        name = row["name"]
        bud = (row["budgeted_cents"] or 0) / 100
        act = (row["activity_cents"] or 0) / 100
        avail = (row["available_cents"] or 0) / 100
        print(f"  {name[:35]:<35}  bud=${bud:>8.2f}  act=${act:>+8.2f}  avail=${avail:>+8.2f}")

    print("\nledger_txn rows in June for each used category (uncategorized originally):")
    for cid in used:
        n = con.execute(
            "SELECT COUNT(*) FROM ledger_txn WHERE category_id = ? AND strftime('%Y-%m', posted_date) = '2026-06'",
            (cid,),
        ).fetchone()[0]
        print(f"  {cid[:8]}  has {n} ledger_txn rows in 2026-06")
