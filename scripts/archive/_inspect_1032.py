from bot import storage
with storage.connect("ynab_helper.db") as con:
    print("pt#1032 full row:")
    r = con.execute("SELECT * FROM pending_txn WHERE id = 1032").fetchone()
    for k, v in dict(r).items():
        print(f"  {k} = {v!r}")

    print(f"\nLinked ledger_txn:")
    if r["ynab_txn_id"] and r["ynab_txn_id"].startswith("ledger:"):
        lid = int(r["ynab_txn_id"].split(":")[1])
        lr = con.execute(
            "SELECT id, posted_date, amount_cents, payee, category_id, "
            "       source_signal, source_email_id, created_at "
            "FROM ledger_txn WHERE id = ?", (lid,)
        ).fetchone()
        if lr:
            for k, v in dict(lr).items():
                print(f"  {k} = {v!r}")
