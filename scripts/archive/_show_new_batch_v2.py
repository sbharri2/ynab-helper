from bot import storage
from bot.batch_processor import build_batch, render_batch_body
items = build_batch("ynab_helper.db", user_id="steven")
with storage.connect("ynab_helper.db") as con:
    total = con.execute(
        "SELECT COUNT(*) FROM pending_txn WHERE assigned_to_user_id='steven' "
        "AND status='pending' AND queue_lane='cold'"
    ).fetchone()[0]
payload = [{"n": i+1, "pt_id": it["pt_id"], "checked": True}
            for i, it in enumerate(items)]
# Simulate user flagging rows 3 and 7
for it in payload:
    if it["n"] in (3, 7):
        it["checked"] = False
print(render_batch_body(items, total, payload))
