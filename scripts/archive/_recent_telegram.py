"""Reconstruct the bot's Telegram activity from the local DB.

Pulls:
  - pending_txn / pending_order rows pushed in the last 6 hours
  - audit_log events that correspond to user actions
    (categorized / skipped / ignored_dm_skipped / queue_unstuck /
     daily_summary_sent / weekly_summary_sent)
  - current bot_conversation state (in-flight question, last DM msg id)
"""
from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone
from bot import storage

CUTOFF_HRS = 6
cutoff_utc = (datetime.now(timezone.utc) - timedelta(hours=CUTOFF_HRS)).isoformat()

with storage.connect("ynab_helper.db") as con:
    print(f"Window: last {CUTOFF_HRS} hours (since {cutoff_utc[:19]} UTC)\n")

    print("[1] bot_conversation state right now:")
    rows = con.execute(
        "SELECT user_id, chat_id, last_asked_id, last_asked_kind, "
        "last_asked_message_id, quiet_until FROM bot_conversation"
    ).fetchall()
    for r in rows:
        print(f"  {dict(r)}")

    print(f"\n[2] pending_txn rows pushed in last {CUTOFF_HRS}h (newest first):")
    rows = con.execute(
        "SELECT id, txn_date, payee, amount_cents, status, suggested_category, "
        "       chosen_category, last_pushed_at "
        "FROM pending_txn "
        "WHERE last_pushed_at >= ? "
        "ORDER BY last_pushed_at DESC LIMIT 25",
        (cutoff_utc,),
    ).fetchall()
    if not rows:
        print("  (none — bot hasn't pushed any pending_txn DMs in this window)")
    for r in rows:
        amt = int(r["amount_cents"] or 0) / 100
        sug = "yes" if r["suggested_category"] else "NO"
        cho = "✓" if r["chosen_category"] else " "
        print(
            f"  #{r['id']:>4}  {r['txn_date']}  ${amt:>10,.2f}  "
            f"status={r['status']:<11}  sug={sug:<3}  chosen={cho}  "
            f"pushed={str(r['last_pushed_at'])[:19]}  "
            f"{(r['payee'] or '')[:30]}"
        )

    print(f"\n[3] pending_order rows pushed in last {CUTOFF_HRS}h:")
    rows = con.execute(
        "SELECT id, source, external_id, total_cents, status, suggested_category, "
        "       chosen_category, raw_summary, last_pushed_at "
        "FROM pending_order "
        "WHERE last_pushed_at >= ? "
        "ORDER BY last_pushed_at DESC LIMIT 15",
        (cutoff_utc,),
    ).fetchall()
    if not rows:
        print("  (none)")
    for r in rows:
        amt = int(r["total_cents"] or 0) / 100
        sum_short = (r["raw_summary"] or "")[:40]
        print(
            f"  #{r['id']:>4}  {r['source']:<18}  ${amt:>10,.2f}  "
            f"status={r['status']:<11}  pushed={str(r['last_pushed_at'])[:19]}  "
            f"{sum_short}"
        )

    print(f"\n[4] Audit events in last {CUTOFF_HRS}h that imply user action:")
    rows = con.execute(
        "SELECT id, event, details, ts FROM audit_log "
        "WHERE ts >= ? "
        "  AND event IN ('categorized','skipped','ignored_dm_skipped',"
        "                'daily_summary_sent','weekly_summary_sent',"
        "                'queue_unstuck','quiet_started','quiet_ended') "
        "ORDER BY id DESC LIMIT 40",
        (cutoff_utc,),
    ).fetchall()
    if not rows:
        print("  (none)")
    for r in rows:
        try:
            d = json.loads(r["details"] or "{}")
        except Exception:
            d = {"raw": r["details"]}
        short = ", ".join(f"{k}={v}" for k, v in list(d.items())[:4])
        print(f"  #{r['id']:>5} {str(r['ts'])[:19]}  {r['event']:<24}  {short[:90]}")
