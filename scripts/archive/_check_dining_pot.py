from bot import storage
CIDS = {
    "f4b8c004-92d9-4aa4-bef9-96f097fe2586": "Dining Out/Entertainment",
    "f1033dd3-4fc4-410e-b75f-0fa6b902d624": "Groceries",
    "c256e9ec-cf91-432f-a448-374f663f4028": "Transportation",
}
with storage.connect("ynab_helper.db") as con:
    print("Current pot state for the most-affected categories:")
    for cid, nm in CIDS.items():
        r = con.execute(
            "SELECT budgeted_cents, activity_cents, available_cents "
            "FROM month_category WHERE month = '2026-06' AND category_id = ?",
            (cid,),
        ).fetchone()
        if not r:
            print(f"  {nm:<28}  (no row)")
            continue
        bud = (r["budgeted_cents"] or 0) / 100
        act = (r["activity_cents"] or 0) / 100
        avail = (r["available_cents"] or 0) / 100
        print(f"  {nm:<28}  budget=${bud:>8.2f}  activity=${act:>+8.2f}  available=${avail:>+8.2f}")

    print("\nIn-flight pending_txns for Dining Out (suggested but unconfirmed):")
    rows = con.execute(
        """SELECT pt.id, pt.payee, pt.amount_cents, pt.suggested_category,
                  lt.category_id AS lt_cat
           FROM pending_txn pt
           JOIN ledger_txn lt ON lt.id = CAST(SUBSTR(pt.ynab_txn_id, 8) AS INTEGER)
           WHERE pt.status = 'pending'
             AND pt.suggested_category = 'f4b8c004-92d9-4aa4-bef9-96f097fe2586'
             AND pt.ynab_txn_id LIKE 'ledger:%'"""
    ).fetchall()
    for r in rows:
        amt = (r["amount_cents"] or 0) / 100
        ltcat = (r["lt_cat"] or "(none)")[:8]
        print(f"  #{r['id']}  ${amt:+.2f}  ledger.cat={ltcat}  {(r['payee'] or '')[:35]}")
