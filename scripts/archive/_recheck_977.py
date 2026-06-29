from bot import storage
with storage.connect("ynab_helper.db") as con:
    print("pending_txn for Amazon.com $38.20 near 6/15:")
    rows = con.execute(
        """SELECT id, txn_date, payee, amount_cents, raw_summary, status,
                  suggested_category, ynab_txn_id, created_at, last_pushed_at,
                  assigned_to_user_id
           FROM pending_txn
           WHERE ABS(amount_cents) BETWEEN 3700 AND 4400
             AND txn_date BETWEEN '2026-06-14' AND '2026-06-17'
             AND (LOWER(payee) LIKE '%amazon%' OR LOWER(payee) LIKE '%amzn%')
           ORDER BY id"""
    ).fetchall()
    for r in rows:
        d = dict(r)
        print(f"\n  #{d['id']}  ${d['amount_cents']/100:+.2f}  status={d['status']}  "
              f"assigned_to={d['assigned_to_user_id']}")
        print(f"     created   = {d['created_at']}")
        print(f"     pushed    = {d['last_pushed_at']}")
        print(f"     payee     = {d['payee']!r}")
        print(f"     ynab_id   = {d['ynab_txn_id']}")
        print(f"     raw_sum   = {(d['raw_summary'] or '')[:100]!r}")
        cat_id = d['suggested_category']
        cat_name = "(none)"
        if cat_id:
            cr = con.execute("SELECT name FROM category WHERE id = ?", (cat_id,)).fetchone()
            if cr:
                cat_name = cr['name']
        print(f"     suggested = {cat_name}")

    print("\nbot_conversation:")
    r = con.execute(
        "SELECT chat_id, last_asked_kind, last_asked_id, last_asked_message_id, last_action_at "
        "FROM bot_conversation"
    ).fetchone()
    print(f"  {dict(r)}")

    print("\nAudit events for #977 and pushes since 6/17:")
    rows = con.execute(
        "SELECT id, event, details, ts FROM audit_log "
        "WHERE ts >= '2026-06-17 00:00:00' "
        "  AND (details LIKE '%977%' OR event IN ('ynab_full_sync_run', 'manual_resuggest', 'retroactive_phase2')) "
        "ORDER BY id DESC LIMIT 12"
    ).fetchall()
    for r in rows:
        print(f"  #{r['id']} {str(r['ts'])[:19]} {r['event']:<26} {(r['details'] or '')[:100]}")
