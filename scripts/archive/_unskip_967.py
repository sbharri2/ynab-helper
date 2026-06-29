from bot import storage
with storage.connect("ynab_helper.db") as con:
    # Confirm the row's enriched raw_summary survived
    r = con.execute(
        "SELECT id, status, suggested_category, raw_summary FROM pending_txn WHERE id = 967"
    ).fetchone()
    print(f"before: {dict(r)}")
    con.execute(
        "UPDATE pending_txn SET status = 'pending', last_pushed_at = NULL WHERE id = 967"
    )
    r = con.execute(
        "SELECT id, status, suggested_category, raw_summary FROM pending_txn WHERE id = 967"
    ).fetchone()
    print(f"after:  {dict(r)}")

storage.audit("ynab_helper.db", "unskip_967", {"reason": "user wants to see enriched DM"})
print("done — push_loop will pick it up within 10s")
