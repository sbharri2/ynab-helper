from bot import storage
storage.init_db("ynab_helper.db")
with storage.connect("ynab_helper.db") as con:
    cols = [dict(r)["name"] for r in con.execute("PRAGMA table_info(pending_txn)").fetchall()]
    print(f"pending_txn has assigned_to_user_id: {'assigned_to_user_id' in cols}")
    cols = [dict(r)["name"] for r in con.execute("PRAGMA table_info(pending_order)").fetchall()]
    print(f"pending_order has assigned_to_user_id: {'assigned_to_user_id' in cols}")

    print("\npending_txn assignee distribution:")
    for r in con.execute("SELECT assigned_to_user_id, COUNT(*) AS n FROM pending_txn GROUP BY assigned_to_user_id"):
        print(f"  {dict(r)}")
    print("pending_order assignee distribution:")
    for r in con.execute("SELECT assigned_to_user_id, COUNT(*) AS n FROM pending_order GROUP BY assigned_to_user_id"):
        print(f"  {dict(r)}")
