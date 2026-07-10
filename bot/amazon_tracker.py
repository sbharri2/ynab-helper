"""Amazon-specific tracking + aged-out alerts.

Per Steven's directive (2026-06-26): Amazon items must never auto-process
without order details. They sit in HOLD until the matching order email
arrives, and after 14 days unmatched they get surfaced as a dedicated
alert so the user can categorize them manually.

This module is the single source of truth for "what's the state of
Amazon items?" — used by both the /amazon Telegram command and the
daily aged-out DM.
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

from bot import storage

log = logging.getLogger(__name__)


def _fmt(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


def get_tracker_snapshot(db_path: Path | str) -> dict:
    """Read-only summary of Amazon items across all states.

    Returns:
        {
          "held": [pt_row, ...],       # waiting for order email (lane=hold)
          "aged_out": [pt_row, ...],   # >14d unmatched, now lane=cold
          "matched_recent": int,        # categorized in the last 7 days
        }
    """
    with storage.connect(db_path) as con:
        held = con.execute(
            """SELECT id, txn_date, amount_cents, payee, raw_summary,
                      lane_changed_at, created_at
               FROM pending_txn
               WHERE status='pending' AND queue_lane='hold'
                 AND (UPPER(payee) LIKE '%AMAZON%' OR UPPER(payee) LIKE '%AMZN%')
               ORDER BY txn_date ASC"""
        ).fetchall()
        aged_out = con.execute(
            """SELECT pt.id, pt.txn_date, pt.amount_cents, pt.payee,
                      pt.raw_summary, pt.lane_changed_at
               FROM pending_txn pt
               WHERE pt.status='pending' AND pt.queue_lane='cold'
                 AND (UPPER(pt.payee) LIKE '%AMAZON%'
                      OR UPPER(pt.payee) LIKE '%AMZN%')
                 AND pt.id IN (
                     SELECT pt_id FROM (
                       SELECT json_extract(value, '$') AS pt_id
                       FROM audit_log,
                            json_each(json_extract(details, '$.pt_ids'))
                       WHERE event = 'amazon_aged_out'
                     )
                 )
               ORDER BY pt.txn_date ASC"""
        ).fetchall()
        # Matched-in-last-7d Amazon categorize taps
        recent = con.execute(
            """SELECT COUNT(*) FROM pending_txn
               WHERE status='categorized'
                 AND chosen_at >= datetime('now', '-7 days')
                 AND (UPPER(payee) LIKE '%AMAZON%'
                      OR UPPER(payee) LIKE '%AMZN%')"""
        ).fetchone()[0]
    return {
        "held": [dict(r) for r in held],
        "aged_out": [dict(r) for r in aged_out],
        "matched_recent": int(recent),
    }


def format_tracker_message(snapshot: dict) -> str:
    """Render /amazon command output. Always returns a non-empty body."""
    held = snapshot["held"]
    aged = snapshot["aged_out"]
    matched = snapshot["matched_recent"]

    lines = ["📦 Amazon tracker", ""]

    if not held and not aged and not matched:
        lines.append("Nothing tracked. 🎉")
        return "\n".join(lines)

    lines.append(f"Last 7 days: {matched} matched & categorized")

    if held:
        lines.append("")
        lines.append(f"⏳ Waiting for order email ({len(held)}):")
        for r in held:
            amt = (r["amount_cents"] or 0) / 100
            d = str(r["txn_date"])[:10]
            payee = (r["payee"] or "")[:30]
            age_anchor = r.get("lane_changed_at") or r.get("created_at")
            age_str = ""
            if age_anchor:
                try:
                    age_dt = age_anchor if isinstance(age_anchor, datetime) \
                        else datetime.fromisoformat(str(age_anchor))
                    if age_dt.tzinfo is None:
                        age_dt = age_dt.replace(tzinfo=timezone.utc)
                    days = (datetime.now(timezone.utc) - age_dt).days
                    age_str = f"  ({days}d)"
                except Exception:  # noqa: BLE001
                    pass
            lines.append(f"  pt#{r['id']:>4}  {d}  ${amt:>+8.2f}  {payee}{age_str}")

    if aged:
        lines.append("")
        lines.append(f"⚠ Aged-out unreconciled ({len(aged)}):")
        for r in aged:
            amt = (r["amount_cents"] or 0) / 100
            d = str(r["txn_date"])[:10]
            payee = (r["payee"] or "")[:30]
            lines.append(f"  pt#{r['id']:>4}  {d}  ${amt:>+8.2f}  {payee}")
        lines.append("")
        lines.append("These won't get an order email — categorize them from the Inbox.")

    return "\n".join(lines)


async def send_aged_out_alert_if_new(app) -> None:
    """If any Amazon item just aged out (audit event 'amazon_aged_out' in
    the last ~24h that we haven't DM'd yet), send a one-time alert.

    Hooked into the daily summary loop right after ynab_qa. Uses an
    'amazon_aged_out_alerted' audit event as the dedup signal.
    """
    from bot.telegram_bot import _in_quiet_hours
    from datetime import datetime as _dt

    settings = app.bot_data["settings"]
    db_path = settings.paths.database
    with storage.connect(db_path) as con:
        # Pull recent aged-out audit events
        recent_aged = con.execute(
            "SELECT id, details, ts FROM audit_log "
            "WHERE event = 'amazon_aged_out' "
            "  AND ts >= datetime('now', '-1 day') "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not recent_aged:
            return
        # Have we already alerted for THIS audit event? Look for a later
        # 'amazon_aged_out_alerted' that references this aged-out id.
        already = con.execute(
            "SELECT 1 FROM audit_log "
            "WHERE event = 'amazon_aged_out_alerted' "
            "  AND details LIKE ?",
            (f"%\"source_audit_id\": {recent_aged['id']}%",),
        ).fetchone()
    if already:
        return

    snapshot = get_tracker_snapshot(db_path)
    if not snapshot["aged_out"]:
        return

    body = format_tracker_message(snapshot)
    body = (f"⚠ {len(snapshot['aged_out'])} Amazon items unreconciled "
            f"after 14 days.\n\n" + body)

    # Steven-only ops alert. Amazon aged-out items are for the operator to
    # investigate (unmatched CC charges, broken parsers) — Allison shouldn't
    # get pinged about ledger drift she can't act on.
    # Redesign-v2 (2026-07-09): group configured → send there instead.
    from bot.group_chat import report_target
    target = report_target(app)
    sent = 0
    if target is not None:
        gbot, group_id = target
        try:
            await gbot.send_message(chat_id=group_id, text=body)
            sent = 1
        except Exception as e:  # noqa: BLE001
            log.warning("amazon aged-out group send failed: %s", e)
    else:
        user_to_chat = {a.user_id: a.chat_id for a in settings.gmail_accounts}
        chat_id = user_to_chat.get("steven")
        if chat_id:
            try:
                from bot.telegram_bot import _bot_for_chat
                await _bot_for_chat(app, chat_id).send_message(
                    chat_id=chat_id, text=body,
                )
                sent = 1
            except Exception as e:  # noqa: BLE001
                log.warning("amazon aged-out send to steven failed: %s", e)
    storage.audit(db_path, "amazon_aged_out_alerted", {
        "source_audit_id": recent_aged["id"], "sent": sent,
    })
