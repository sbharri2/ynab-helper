"""What's the bot doing TODAY (2026-06-15)? Has any background loop fired?"""
from __future__ import annotations
import json
from collections import Counter
from bot import storage

# Start of today UTC. Today is 2026-06-15.
TODAY_START = "2026-06-15 00:00:00"

with storage.connect("ynab_helper.db") as con:
    print("[1] bot_conversation right now:")
    r = con.execute(
        "SELECT chat_id, user_id, last_asked_id, last_asked_kind, "
        "last_action_at, last_asked_message_id, "
        "length(last_turns_json) AS turns_len "
        "FROM bot_conversation"
    ).fetchone()
    print(f"  {dict(r)}")

    print("\n[2] audit_log event counts today:")
    rows = con.execute(
        "SELECT event, COUNT(*) AS n FROM audit_log "
        "WHERE ts >= ? GROUP BY event ORDER BY n DESC",
        (TODAY_START,),
    ).fetchall()
    for r in rows:
        print(f"  {r['event']:<30}  {r['n']}")
    if not rows:
        print("  (NOTHING in audit_log since 2026-06-15 00:00 UTC)")

    print("\n[3] Most recent audit events of all kinds (last 10):")
    rows = con.execute(
        "SELECT id, event, details, ts FROM audit_log ORDER BY id DESC LIMIT 10"
    ).fetchall()
    for r in rows:
        try:
            d = json.loads(r["details"] or "{}")
        except Exception:
            d = {"raw": r["details"]}
        short = ", ".join(f"{k}={v}" for k, v in list(d.items())[:4])
        print(f"  #{r['id']:>5} {str(r['ts'])[:19]}  {r['event']:<26}  {short[:90]}")

    print("\n[4] Any pending_order / pending_txn updated_at since today?")
    n_orders = con.execute(
        "SELECT COUNT(*) FROM pending_order WHERE updated_at >= ?",
        (TODAY_START,),
    ).fetchone()[0]
    n_txns = con.execute(
        "SELECT COUNT(*) FROM pending_txn WHERE created_at >= ? OR chosen_at >= ?",
        (TODAY_START, TODAY_START),
    ).fetchone()[0]
    print(f"  pending_order updated:  {n_orders}")
    print(f"  pending_txn touched:    {n_txns}")

    print("\n[5] Last ledger_ingest event timestamp:")
    r = con.execute(
        "SELECT ts FROM audit_log WHERE event='ledger_ingest' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    print(f"  {dict(r) if r else '(none ever)'}")

    print("\n[6] Last sample_collector_run event timestamp:")
    r = con.execute(
        "SELECT ts FROM audit_log WHERE event='sample_collector_run' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    print(f"  {dict(r) if r else '(none ever)'}")
