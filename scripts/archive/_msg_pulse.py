"""Quick pulse-check: is the bot pushing right now?"""
from __future__ import annotations
import json
from bot import storage

with storage.connect("ynab_helper.db") as con:
    print("bot_conversation:")
    rows = con.execute(
        "SELECT user_id, last_asked_id, last_asked_kind, last_asked_message_id, "
        "quiet_until FROM bot_conversation"
    ).fetchall()
    for r in rows:
        print(f"  {dict(r)}")

    print("\nRecent push-related audit events (last 25):")
    rows = con.execute(
        "SELECT id, event, details, ts FROM audit_log "
        "WHERE event IN ('ignored_dm_skipped','categorized','skipped',"
        "                'daily_summary_sent','weekly_summary_sent',"
        "                'pending_order_inserted','queue_unstuck') "
        "ORDER BY id DESC LIMIT 25"
    ).fetchall()
    for r in rows:
        try:
            d = json.loads(r["details"] or "{}")
        except Exception:
            d = {"raw": r["details"]}
        short = ", ".join(f"{k}={v}" for k, v in list(d.items())[:4])
        print(f"  #{r['id']:>5} {str(r['ts'])[:19]}  {r['event']:<24}  {short[:90]}")

    print("\npending_txn — rows with last_pushed_at in last 10 min:")
    rows = con.execute(
        "SELECT id, payee, amount_cents, suggested_category, last_pushed_at "
        "FROM pending_txn "
        "WHERE last_pushed_at IS NOT NULL "
        "  AND datetime(last_pushed_at) > datetime('now','-10 minutes') "
        "ORDER BY last_pushed_at DESC"
    ).fetchall()
    for r in rows:
        sug = "yes" if r["suggested_category"] else "NO"
        amt = int(r["amount_cents"] or 0) / 100
        print(f"  #{r['id']:>4}  ${amt:>10,.2f}  sug={sug:<3}  "
              f"pushed={r['last_pushed_at']}  {(r['payee'] or '')[:30]}")
