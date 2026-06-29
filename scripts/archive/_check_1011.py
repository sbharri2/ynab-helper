from bot import storage
with storage.connect("ynab_helper.db") as con:
    r = con.execute("SELECT id, queue_lane, status, payee, amount_cents, txn_date FROM pending_txn WHERE id=1011").fetchone()
    print(dict(r))
storage.audit("ynab_helper.db", "amazon_demoted_to_hold", {"count": 1, "pt_ids": [1011]})
print("audit logged")
