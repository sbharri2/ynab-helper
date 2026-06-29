from bot import storage
from bot.batch_processor import build_batch, render_batch_message

with storage.connect("ynab_helper.db") as con:
    cnt = con.execute(
        "SELECT queue_lane, COUNT(*) AS n FROM pending_txn "
        "WHERE status='pending' GROUP BY queue_lane"
    ).fetchall()
    for r in cnt:
        print(f"  {dict(r)}")

items = build_batch("ynab_helper.db", user_id="steven")
print(f"\nBuilt batch with {len(items)} items.")
if items:
    with storage.connect("ynab_helper.db") as con:
        total = con.execute(
            "SELECT COUNT(*) FROM pending_txn "
            "WHERE assigned_to_user_id='steven' AND status='pending' "
            "  AND queue_lane='cold'"
        ).fetchone()[0]
    print("\n" + render_batch_message(items, total))
