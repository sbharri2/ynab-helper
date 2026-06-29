from bot import storage

with storage.connect("ynab_helper.db") as con:
    print("bot_conversation state:")
    r = con.execute(
        "SELECT chat_id, last_asked_kind, last_asked_id, "
        "last_asked_message_id, last_action_at "
        "FROM bot_conversation"
    ).fetchone()
    print(f"  {dict(r)}")

    print("\npending_txn #967 state:")
    r = con.execute(
        "SELECT id, txn_date, payee, amount_cents, raw_summary, status, "
        "suggested_category, last_pushed_at FROM pending_txn WHERE id = 967"
    ).fetchone()
    print(f"  {dict(r)}")

    print("\nQueue head — next 10 by id, oldest first:")
    rows = con.execute(
        """SELECT id, txn_date, payee, amount_cents, status, suggested_category, last_pushed_at
           FROM pending_txn
           WHERE status = 'pending'
           ORDER BY id ASC LIMIT 10"""
    ).fetchall()
    for r in rows:
        amt = (r["amount_cents"] or 0) / 100
        sug = (r["suggested_category"] or "(none)")[:8]
        print(f"  #{r['id']}  {r['txn_date']}  ${amt:+.2f}  sug={sug}  "
              f"pushed={r['last_pushed_at']}  {(r['payee'] or '')[:30]}")

    print("\npending_order queue (orders go first):")
    rows = con.execute(
        """SELECT id, source, order_date, total_cents, status
           FROM pending_order WHERE status = 'pending'
           ORDER BY id ASC LIMIT 5"""
    ).fetchall()
    for r in rows:
        amt = (r["total_cents"] or 0) / 100
        print(f"  #{r['id']}  {r['source']}  {r['order_date']}  ${amt:.2f}  status={r['status']}")
