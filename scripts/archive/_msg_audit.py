"""Audit the messaging system: what the bot has been sending vs what the
design says it should be sending.

Reads from audit_log, bot_conversation, pending_txn, pending_order. No
external calls. No mutations.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from bot import storage
from bot.config import load_settings

settings = load_settings()
db = settings.paths.database

print("=" * 80)
print("MESSAGING AUDIT")
print("=" * 80)

with storage.connect(db) as con:
    # --- 1. Recent audit_log entries (the bot's own self-report) ---
    print("\n[1] Last 30 audit_log entries (newest first):")
    rows = con.execute(
        """SELECT id, event, details, ts
           FROM audit_log
           ORDER BY id DESC LIMIT 30"""
    ).fetchall()
    for r in rows:
        ts = str(r["ts"])[:19]
        try:
            payload = json.loads(r["details"] or "{}")
        except Exception:
            payload = {"raw": r["details"]}
        short = ", ".join(f"{k}={v}" for k, v in list(payload.items())[:3])
        print(f"  #{r['id']:>5} {ts}  {r['event']:<28}  {short[:80]}")

    # --- 2. bot_conversation state per user ---
    print("\n[2] bot_conversation rows:")
    rows = con.execute("SELECT * FROM bot_conversation").fetchall()
    for r in rows:
        d = dict(r)
        print(f"  user_id={d.get('user_id')}  chat_id={d.get('chat_id')}")
        print(f"    last_asked_id      = {d.get('last_asked_id')}")
        print(f"    last_asked_kind    = {d.get('last_asked_kind')}")
        print(f"    last_asked_at      = {d.get('last_asked_at')}")
        print(f"    quiet_until        = {d.get('quiet_until')}")
        print(f"    last_pushed_at     = {d.get('last_pushed_at')}")
        print(f"    last_summary_date  = {d.get('last_summary_date')}")

    # --- 3. Pending queue health ---
    print("\n[3] pending_txn queue breakdown:")
    rows = con.execute(
        """SELECT status,
                  SUM(CASE WHEN suggested_category IS NULL THEN 1 ELSE 0 END) AS null_sugg,
                  SUM(CASE WHEN suggested_category IS NOT NULL THEN 1 ELSE 0 END) AS has_sugg,
                  COUNT(*) AS total
           FROM pending_txn GROUP BY status"""
    ).fetchall()
    for r in rows:
        print(
            f"  status={r['status']:<12}  total={r['total']:<4}  "
            f"with_sugg={r['has_sugg']:<4}  null_sugg={r['null_sugg']}"
        )

    print("\n[3a] Oldest 5 pending rows (status='pending'):")
    rows = con.execute(
        """SELECT id, txn_date, payee, amount_cents, suggested_category, last_pushed_at
           FROM pending_txn
           WHERE status='pending'
           ORDER BY txn_date ASC, id ASC LIMIT 5"""
    ).fetchall()
    for r in rows:
        sug = "yes" if r["suggested_category"] else "NO"
        amt = int(r["amount_cents"] or 0) / 100
        print(
            f"  #{r['id']:>4}  {r['txn_date']}  ${amt:>10,.2f}  "
            f"sug={sug:<3}  pushed={r['last_pushed_at'] or '(never)'}  "
            f"{(r['payee'] or '')[:30]}"
        )

    print("\n[3b] Newest 5 pending rows (status='pending'):")
    rows = con.execute(
        """SELECT id, txn_date, payee, amount_cents, suggested_category, last_pushed_at
           FROM pending_txn
           WHERE status='pending'
           ORDER BY id DESC LIMIT 5"""
    ).fetchall()
    for r in rows:
        sug = "yes" if r["suggested_category"] else "NO"
        amt = int(r["amount_cents"] or 0) / 100
        print(
            f"  #{r['id']:>4}  {r['txn_date']}  ${amt:>10,.2f}  "
            f"sug={sug:<3}  pushed={r['last_pushed_at'] or '(never)'}  "
            f"{(r['payee'] or '')[:30]}"
        )

    # --- 4. Pending orders (Amazon/Venmo/retailer) ---
    print("\n[4] pending_order queue breakdown:")
    rows = con.execute(
        """SELECT status, COUNT(*) as n FROM pending_order GROUP BY status"""
    ).fetchall()
    for r in rows:
        print(f"  status={r['status']:<12}  n={r['n']}")

    # --- 5. Did the daily summary fire this morning? ---
    today = date.today()
    yest = today - timedelta(days=1)
    print(f"\n[5] daily_summary_sent events (today + last 3 days):")
    for d in [today, yest, today - timedelta(days=2), today - timedelta(days=3)]:
        rows = con.execute(
            """SELECT id, event, details, ts
               FROM audit_log
               WHERE (event LIKE 'daily_summary%' OR event LIKE 'push%'
                      OR event LIKE 'summary%')
                 AND DATE(ts) = ?""",
            (d.isoformat(),),
        ).fetchall()
        print(f"  {d}: {len(rows)} events")
        for r in rows[:3]:
            print(f"    #{r['id']}  {r['event']}  {str(r['ts'])[:19]}")

    # --- 6. Look at recent ledger ingestion to confirm IMAP path is live ---
    print(f"\n[6] Recent ledger_txn ingest activity (last 7 days):")
    cutoff = (today - timedelta(days=7)).isoformat()
    rows = con.execute(
        """SELECT DATE(created_at) as d, source_signal, COUNT(*) as n
           FROM ledger_txn
           WHERE created_at >= ?
           GROUP BY DATE(created_at), source_signal
           ORDER BY d DESC, n DESC""",
        (cutoff,),
    ).fetchall()
    for r in rows[:25]:
        print(f"  {r['d']}  {r['source_signal'] or '(none)':<30}  n={r['n']}")

print("\n" + "=" * 80)
print("DONE")
