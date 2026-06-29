"""Show what the current /batch view actually contains right now,
plus the audit history for the rows the user is seeing as duplicates.
"""
from bot import storage
from bot.batch_processor import build_batch

items = build_batch("ynab_helper.db", user_id="steven")
print(f"Current /batch would return {len(items)} items:\n")
for i, it in enumerate(items, start=1):
    amt = it["amount_cents"] / 100
    print(f"  {i:>2}. pt#{it['pt_id']:>4}  {it['txn_date']}  ${amt:+8.2f}  "
          f"{(it['payee'] or '')[:32]:<32}  → {it.get('suggested_category_name') or '(no guess)'}")

with storage.connect("ynab_helper.db") as con:
    print("\nLatest batch context saved:")
    r = con.execute(
        "SELECT last_batch_json FROM bot_conversation WHERE user_id = 'steven'"
    ).fetchone()
    if r and r["last_batch_json"]:
        import json
        b = json.loads(r["last_batch_json"])
        print(f"  sent_at: {b.get('sent_at')}")
        print(f"  consumed: {b.get('consumed')}")
        print(f"  items: {b.get('items')}")

    print("\nAudit history for the apparent dupe pairs (last 3 days):")
    for pt_id in (1002, 1005, 1006, 1010, 1018, 1019, 1020):
        rows = con.execute(
            "SELECT id, event, details, ts FROM audit_log "
            "WHERE ts >= datetime('now', '-3 days') "
            "  AND details LIKE ? "
            "ORDER BY id",
            (f"%\"id\": {pt_id}%",),
        ).fetchall()
        if not rows:
            continue
        print(f"\n  pt#{pt_id}:")
        for r in rows:
            print(f"    #{r['id']}  {str(r['ts'])[:19]}  {r['event']:<28}  {(r['details'] or '')[:100]}")
