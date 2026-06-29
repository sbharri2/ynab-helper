from bot import storage
from bot.batch_processor import build_batch, render_batch_message
items = build_batch("ynab_helper.db", user_id="steven")
with storage.connect("ynab_helper.db") as con:
    total = con.execute(
        "SELECT COUNT(*) FROM pending_txn "
        "WHERE assigned_to_user_id='steven' AND status='pending' AND queue_lane='cold'"
    ).fetchone()[0]
print(render_batch_message(items, total))
