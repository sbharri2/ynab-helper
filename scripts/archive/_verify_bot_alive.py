"""After bot restart, verify all background loops + readiness for inbound."""
from bot import storage

with storage.connect("ynab_helper.db") as con:
    print("Audit events since restart (11:44 UTC):")
    rows = con.execute(
        "SELECT id, event, ts FROM audit_log "
        "WHERE ts >= '2026-06-15 11:44:00' "
        "ORDER BY id DESC LIMIT 30"
    ).fetchall()
    counts = {}
    for r in rows:
        counts[r["event"]] = counts.get(r["event"], 0) + 1
    for ev, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {ev:<30}  {n}")
    print()
    print("bot_conversation now:")
    r = con.execute("SELECT chat_id, last_asked_id, last_asked_kind, last_action_at FROM bot_conversation").fetchone()
    print(f"  {dict(r)}")

    print("\nPending queue head (top 5 by id, oldest first):")
    rows = con.execute(
        "SELECT id, txn_date, payee, amount_cents, suggested_category, last_pushed_at "
        "FROM pending_txn WHERE status='pending' "
        "ORDER BY id ASC LIMIT 5"
    ).fetchall()
    for r in rows:
        sug = "yes" if r["suggested_category"] else "NO"
        amt = int(r["amount_cents"] or 0) / 100
        print(f"  #{r['id']:>4}  {r['txn_date']}  ${amt:>10,.2f}  sug={sug}  "
              f"pushed={r['last_pushed_at']}  {(r['payee'] or '')[:30]}")
