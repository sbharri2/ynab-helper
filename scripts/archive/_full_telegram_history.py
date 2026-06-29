"""Full reconstruction of bot's Telegram activity since the restart."""
from __future__ import annotations
import json
from bot import storage

# Restart was 2026-06-14 10:32:13 UTC (6:32 EDT)
SINCE_UTC = "2026-06-14 10:32:00"

with storage.connect("ynab_helper.db") as con:
    print(f"All Telegram-relevant activity since restart ({SINCE_UTC} UTC)\n")

    print("[1] pending_order rows ever pushed:")
    rows = con.execute(
        "SELECT id, source, total_cents, status, raw_summary, "
        "       suggested_category, chosen_category, last_pushed_at "
        "FROM pending_order "
        "WHERE last_pushed_at IS NOT NULL "
        "  AND last_pushed_at >= ? "
        "ORDER BY last_pushed_at DESC",
        (SINCE_UTC,),
    ).fetchall()
    for r in rows:
        amt = int(r["total_cents"] or 0) / 100
        cho = "✓" if r["chosen_category"] else " "
        print(f"  #{r['id']:>4}  ${amt:>10,.2f}  status={r['status']:<11}  "
              f"chosen={cho}  pushed={str(r['last_pushed_at'])[:19]}  "
              f"{(r['raw_summary'] or '')[:40]}")
    if not rows:
        print("  (none)")

    print("\n[2] pending_txn rows ever pushed since restart:")
    rows = con.execute(
        "SELECT id, txn_date, payee, amount_cents, status, "
        "       suggested_category, chosen_category, last_pushed_at "
        "FROM pending_txn "
        "WHERE last_pushed_at IS NOT NULL "
        "  AND last_pushed_at >= ? "
        "ORDER BY last_pushed_at DESC",
        (SINCE_UTC,),
    ).fetchall()
    for r in rows:
        amt = int(r["amount_cents"] or 0) / 100
        cho = "✓" if r["chosen_category"] else " "
        print(f"  #{r['id']:>4}  {r['txn_date']}  ${amt:>10,.2f}  "
              f"status={r['status']:<11}  chosen={cho}  "
              f"pushed={str(r['last_pushed_at'])[:19]}  "
              f"{(r['payee'] or '')[:30]}")
    if not rows:
        print("  (none)")

    print("\n[3] All audit events since restart, except sample_collector/ledger_ingest/ynab_poll:")
    rows = con.execute(
        "SELECT id, event, details, ts FROM audit_log "
        "WHERE ts >= ? "
        "  AND event NOT IN ('sample_collector_run','ledger_ingest','ynab_poll',"
        "                    'pending_order_inserted') "
        "ORDER BY id DESC LIMIT 60",
        (SINCE_UTC,),
    ).fetchall()
    for r in rows:
        try:
            d = json.loads(r["details"] or "{}")
        except Exception:
            d = {"raw": r["details"]}
        short = ", ".join(f"{k}={v}" for k, v in list(d.items())[:5])
        print(f"  #{r['id']:>5} {str(r['ts'])[:19]}  {r['event']:<24}  {short[:90]}")
