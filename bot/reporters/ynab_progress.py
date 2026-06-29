"""Weekly YNAB-sunset progress report.

Sister to the daily YNAB writer report. Where the daily report shows
"what happened today", the weekly report shows TRENDS — is the bot
catching more transactions before YNAB? Are conflicts shrinking? Is
the email-first rate climbing? The goal is a visible gauge so Steven
knows when he can confidently flip ``settings.ynab.mode`` to off.

The numbers come from two sources:

  * ``audit_log`` entries for ``ynab_writer_run`` — the daily writer
    counters (pushed, conflicts, no_ynab_match, etc.). Summed over each
    week.

  * ``ledger_signal`` joined with ``ledger_txn`` — for "email-first
    coverage". A transaction is email-first when at least one of its
    ledger_signal rows has a signal_kind OTHER than ``ynab_sync``
    (chase_alert, citi_alert, coastal_transaction_alert, etc.). When
    the only signal is ``ynab_sync`` the bot's email pipeline missed it.

Compares THIS week (last 7 days ending today) vs THE PREVIOUS week.
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path

from bot import storage

log = logging.getLogger(__name__)


def _fmt_pct(n: int, d: int) -> str:
    if d <= 0:
        return "n/a"
    return f"{n / d * 100:.0f}%"


def _trend_arrow(this_val: float, last_val: float, *, lower_is_better: bool = False) -> str:
    """Compact directional indicator. Used for one-glance trend reading."""
    if this_val == last_val:
        return "→"
    if (this_val > last_val) ^ lower_is_better:
        return "📈"
    return "📉"


def _week_window(end_date: date) -> tuple[date, date]:
    """Return ``(start, end)`` for the 7-day window ending ``end_date``
    (inclusive)."""
    return end_date - timedelta(days=6), end_date


def _collect_writer_counters(
    db_path: Path | str, start: date, end: date,
) -> dict:
    """Sum every ``ynab_writer_run`` audit event in [start, end] (inclusive
    on both sides). One run per day is the expected cadence; if /ynab was
    triggered manually multiple times in a day we sum them all."""
    totals = {
        "pushed": 0, "already_synced": 0, "rewired": 0,
        "no_ynab_match": 0, "conflicts": 0, "failed": 0,
        "runs": 0,
    }
    with storage.connect(db_path) as con:
        rows = con.execute(
            "SELECT details FROM audit_log "
            "WHERE event = 'ynab_writer_run' "
            "  AND date(ts) BETWEEN ? AND ?",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    for r in rows:
        try:
            d = json.loads(r["details"] or "{}")
        except (ValueError, TypeError):
            continue
        totals["runs"] += 1
        for k in ("pushed", "already_synced", "rewired",
                   "no_ynab_match", "conflicts", "failed"):
            totals[k] += int(d.get(k) or 0)
    return totals


def _collect_coverage_stats(
    db_path: Path | str, start: date, end: date,
) -> dict:
    """How many ledger_txn rows for [start, end] had at least one non-
    ynab_sync signal (email-first) vs only ynab_sync (the bot missed it).
    """
    with storage.connect(db_path) as con:
        total_row = con.execute(
            "SELECT COUNT(*) AS n FROM ledger_txn "
            "WHERE posted_date BETWEEN ? AND ? AND parent_txn_id IS NULL",
            (start.isoformat(), end.isoformat()),
        ).fetchone()
        # Email-first: at least one ledger_signal whose signal_kind is
        # NOT 'ynab_sync' / 'ynab_history'.
        email_first_row = con.execute(
            """SELECT COUNT(DISTINCT lt.id) AS n
               FROM ledger_txn lt
               JOIN ledger_signal ls ON ls.ledger_txn_id = lt.id
               WHERE lt.posted_date BETWEEN ? AND ?
                 AND ls.signal_kind NOT IN ('ynab_sync', 'ynab_history')""",
            (start.isoformat(), end.isoformat()),
        ).fetchone()
    total = int(total_row["n"] or 0)
    email_first = int(email_first_row["n"] or 0)
    return {
        "total_txns": total,
        "email_first": email_first,
        "ynab_only": max(0, total - email_first),
    }


def build_progress_report(
    db_path: Path | str, *, as_of: date | None = None,
) -> str:
    """Render the weekly progress DM body."""
    if as_of is None:
        as_of = date.today()
    this_start, this_end = _week_window(as_of)
    last_start, last_end = _week_window(as_of - timedelta(days=7))

    this_w = _collect_writer_counters(db_path, this_start, this_end)
    last_w = _collect_writer_counters(db_path, last_start, last_end)
    this_c = _collect_coverage_stats(db_path, this_start, this_end)
    last_c = _collect_coverage_stats(db_path, last_start, last_end)

    # Coverage % (the headline number)
    this_pct = (this_c["email_first"] / this_c["total_txns"] * 100
                 if this_c["total_txns"] else 0)
    last_pct = (last_c["email_first"] / last_c["total_txns"] * 100
                 if last_c["total_txns"] else 0)

    lines = []
    lines.append(f"📈 YNAB sunset progress — Week of {this_start.isoformat()}")
    lines.append("")

    # Headline: email-first coverage trend
    arrow = _trend_arrow(this_pct, last_pct)
    lines.append(
        f"Email-first coverage: {this_pct:.0f}% "
        f"({this_c['email_first']}/{this_c['total_txns']})  "
        f"{arrow} prev {last_pct:.0f}%"
    )

    # Writer counters
    lines.append("")
    lines.append("This week the daily writer:")
    lines.append(
        f"  ✅ pushed {this_w['pushed']} categorizations to YNAB"
    )
    if this_w["rewired"]:
        lines.append(
            f"  🔗 rewired {this_w['rewired']} synthetic ids to real YNAB UUIDs"
        )
    if this_w["already_synced"]:
        lines.append(
            f"  = {this_w['already_synced']} already in sync (no-op)"
        )
    # Conflict trend
    conflict_arrow = _trend_arrow(
        this_w["conflicts"], last_w["conflicts"], lower_is_better=True,
    )
    lines.append(
        f"  ⚠ {this_w['conflicts']} conflicts (bot overrode YNAB)  "
        f"{conflict_arrow} prev {last_w['conflicts']}"
    )
    # No-match trend
    nomatch_arrow = _trend_arrow(
        this_w["no_ynab_match"], last_w["no_ynab_match"], lower_is_better=True,
    )
    lines.append(
        f"  ⏳ {this_w['no_ynab_match']} still waiting for YNAB to surface  "
        f"{nomatch_arrow} prev {last_w['no_ynab_match']}"
    )
    if this_w["failed"]:
        lines.append(f"  ❌ {this_w['failed']} push failures")

    # Interpretive footer
    lines.append("")
    if this_pct >= 95 and this_w["conflicts"] <= 2 and this_w["no_ynab_match"] <= 2:
        lines.append("✅ Bot is consistently catching YNAB. Ready to flip mode → read_only.")
    elif this_w["runs"] == 0:
        lines.append(
            "🆕 No daily writer runs in this window — first weekly report. "
            "Next week's comparison will be more meaningful."
        )
    elif this_pct >= last_pct:
        lines.append("🟢 Coverage trending up — bot is gaining on YNAB.")
    else:
        lines.append(
            "🟡 Coverage dropped this week — look at which payees the bot missed."
        )
    return "\n".join(lines)


async def send_progress_report(app) -> None:
    """DM the weekly progress report to opted-in users."""
    settings = app.bot_data["settings"]
    db_path = settings.paths.database
    body = build_progress_report(db_path)
    recipients = storage.list_recipients_for_period(db_path, "weekly")
    user_to_chat = {a.user_id: a.chat_id for a in settings.gmail_accounts}
    sent = skipped = 0
    for r in recipients:
        chat_id = user_to_chat.get(r["user_id"])
        if not chat_id:
            skipped += 1
            continue
        try:
            from bot.telegram_bot import _bot_for_chat
            await _bot_for_chat(app, chat_id).send_message(
                chat_id=chat_id, text=body,
            )
            sent += 1
        except Exception as e:  # noqa: BLE001
            log.warning("ynab_progress send to %s failed: %s",
                         r["user_id"], e)
            skipped += 1
    storage.audit(db_path, "ynab_progress_sent",
                  {"sent": sent, "skipped": skipped})
