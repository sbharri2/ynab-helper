"""Phase 5 — weekly Sunday-evening summary.

For each recipient with `user_pref.receives_weekly=1` and outside their quiet
hours, build a 7-day retrospective: total spend, top categories, top payees,
anomalies (vs 12-week median), and upcoming-bills heads-up.
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import date, timedelta

from bot import anomaly, storage

log = logging.getLogger(__name__)


def _fmt(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


def build_weekly_summary(db_path: str, *, as_of: date | None = None) -> str:
    today = as_of or date.today()
    week_start = today - timedelta(days=7)
    month = today.strftime("%Y-%m")

    with storage.connect(db_path) as con:
        # 7-day rows
        rows = con.execute(
            """SELECT posted_date, amount_cents, payee, category_id
               FROM ledger_txn
               WHERE posted_date >= ?""",
            (week_start,),
        ).fetchall()

        # Categories burning fast this month
        # (Imported as call into bot.anomaly.categories_burning_fast)

        # Roll up by category for the week
        cat_totals: defaultdict[str | None, int] = defaultdict(int)
        payee_totals: Counter = Counter()
        for r in rows:
            amt = int(r["amount_cents"])
            cat_totals[r["category_id"]] += amt
            if amt < 0 and r["payee"]:
                payee_totals[r["payee"]] += amt

        # Look up category names for the top-spent ones
        cat_names = {}
        if cat_totals:
            qs = "(" + ",".join("?" * len([k for k in cat_totals if k])) + ")"
            valid_ids = [k for k in cat_totals if k]
            if valid_ids:
                for r in con.execute(
                    f"SELECT id, name FROM category WHERE id IN {qs}",
                    valid_ids,
                ):
                    cat_names[r["id"]] = r["name"]

    lines: list[str] = []
    lines.append(f"📅 Week of {week_start.strftime('%b %d')} → {today.strftime('%b %d')}")
    lines.append("")

    if not rows:
        lines.append("No transactions this week.")
        return "\n".join(lines)

    total_outflow = -sum(int(r["amount_cents"]) for r in rows if int(r["amount_cents"]) < 0)
    total_inflow = sum(int(r["amount_cents"]) for r in rows if int(r["amount_cents"]) >= 0)
    lines.append(
        f"📊 {len(rows)} transactions | spent {_fmt(-total_outflow)} | in {_fmt(total_inflow)}"
    )

    # Top spending categories
    spending_cats = sorted(
        [(cid, total) for cid, total in cat_totals.items() if total < 0],
        key=lambda x: x[1],
    )[:5]
    if spending_cats:
        lines.append("")
        lines.append("🏷 Top spending categories:")
        for cid, total in spending_cats:
            name = cat_names.get(cid, "(uncategorized)") if cid else "(uncategorized)"
            lines.append(f"  {_fmt(total):>10}  {name}")

    # Top payees by spend (sort ASC since amounts are negative outflows).
    top_payees = sorted(payee_totals.items(), key=lambda kv: kv[1])[:5]
    if top_payees:
        lines.append("")
        lines.append("🛒 Top payees:")
        for payee, total in top_payees:
            lines.append(f"  {_fmt(total):>10}  {payee}")

    # Anomalies — check the biggest-spend payees this week
    payees_to_check = [p for p, _ in
                       sorted(payee_totals.items(), key=lambda kv: kv[1])[:10]]
    anomalies: list[dict] = []
    for p in payees_to_check:
        result = anomaly.z_score_for_payee(db_path, p, window_weeks=12)
        if result["z"] is not None and abs(result["z"]) > 2.0:
            anomalies.append(result)
    if anomalies:
        lines.append("")
        lines.append("⚠️  Unusual this week:")
        for a in anomalies[:3]:
            direction = "higher" if a["this_week_cents"] < a["median_cents"] else "lower"
            # Recall amounts are negative for outflows. More-negative = bigger spend.
            spend = -a["this_week_cents"] if a["this_week_cents"] < 0 else 0
            median_spend = -a["median_cents"] if a["median_cents"] < 0 else 0
            # Determine direction in spend terms
            if spend > median_spend:
                dir_str = "above"
            else:
                dir_str = "below"
            lines.append(
                f"  {a['payee']}: {_fmt(-spend)} spent, {dir_str} typical "
                f"({_fmt(-median_spend)} median)"
            )

    # Categories burning fast
    burning = anomaly.categories_burning_fast(db_path, month)
    if burning:
        lines.append("")
        lines.append("🔥 On pace to overspend this month:")
        for b in burning[:3]:
            lines.append(
                f"  {b['category_name']}: {_fmt(b['spent_cents'])} of "
                f"{_fmt(b['budgeted_cents'])} budgeted "
                f"(pace {b['pace_ratio']}x — projects to {_fmt(b['projected_total_cents'])})"
            )

    return "\n".join(lines)


async def send_weekly_summaries(app) -> None:
    """Sunday-evening fan-out to opted-in recipients."""
    settings = app.bot_data["settings"]
    db_path = settings.paths.database

    recipients = storage.list_recipients_for_period(db_path, "weekly")
    if not recipients:
        log.info("weekly: no recipients opted in")
        return

    text = build_weekly_summary(db_path)
    user_to_chat = {acct.user_id: acct.chat_id for acct in settings.gmail_accounts}

    from datetime import datetime as _dt
    now = _dt.now()
    from bot.telegram_bot import _in_quiet_hours

    sent = 0
    skipped = 0
    for r in recipients:
        chat_id = user_to_chat.get(r["user_id"])
        if chat_id is None:
            skipped += 1
            continue
        if _in_quiet_hours(now, r["quiet_hours"]):
            skipped += 1
            continue
        try:
            await app.bot.send_message(chat_id=chat_id, text=text)
            sent += 1
        except Exception as e:  # noqa: BLE001
            log.warning("weekly: send to %s failed: %s", r["user_id"], e)
            skipped += 1

    storage.audit(db_path, "weekly_summary_sent",
                  {"sent": sent, "skipped": skipped, "recipients": len(recipients)})
