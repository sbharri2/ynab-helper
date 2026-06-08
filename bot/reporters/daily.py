"""Phase 5 — daily morning summary.

Push at 07:30 local (configurable). For each recipient who has
`user_pref.receives_daily=1` and is outside their quiet hours, build and DM
a brief summary of:
  - Yesterday's activity (count + total + top movers)
  - Current month's overspent and "tight" categories
  - Account balances (cleared)
  - Today's upcoming scheduled bills (when scheduled-bill data exists)

Pure functions for the build; the orchestrator handles fan-out + delivery.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import date, timedelta

from bot import storage

log = logging.getLogger(__name__)


def _fmt(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


_TRAILING_COUNTRY_RE = re.compile(r"\s+USA\s*$", re.I)


def _clean_payee(raw: str) -> str:
    """Pretty up a bank/CC-style ALL-CAPS merchant string for display.

    Strips the trailing " USA" suffix (Citi appends it). Title-cases
    each word so "APPLE.COM/BILL CUPERTINO" becomes "Apple.com/bill
    Cupertino" and "LA TABERNA APEX" becomes "La Taberna Apex".

    We intentionally do NOT try to strip city/state — distinguishing
    "APEX" (a city) from "TABERNA" (part of the merchant name) in
    all-caps text without a gazetteer guesses wrong as often as right.
    """
    if not raw:
        return raw
    s = _TRAILING_COUNTRY_RE.sub("", raw.strip())
    # Python's str.capitalize() lowercases everything after the first
    # character, which is exactly the right rule for "APPLE.COM" →
    # "Apple.com" and "LA" → "La".
    return " ".join(w.capitalize() for w in s.split())


def build_daily_summary(db_path: str, *, as_of: date | None = None) -> str:
    """Returns the morning-summary text. No I/O beyond SQLite reads."""
    today = as_of or date.today()
    yesterday = today - timedelta(days=1)
    month = today.strftime("%Y-%m")

    with storage.connect(db_path) as con:
        # Yesterday's activity (excluding transfers — no transfer flag yet but
        # all our ledger_txn entries have no transfer_account_id concept;
        # ledger_txn already excludes those by Phase 2 design.)
        rows_y = con.execute(
            """SELECT amount_cents, payee, category_id
               FROM ledger_txn
               WHERE posted_date = ?""",
            (yesterday,),
        ).fetchall()

        # Account balances. Prefer the most recent `account_balance_observed`
        # row (bank-emailed) when present; fall back to the YNAB-imported
        # balance otherwise. Summing ledger_txn doesn't work here because the
        # import skipped transfers, so the math wouldn't reconcile.
        accts = con.execute(
            """SELECT a.id, a.name, a.type, a.on_budget,
                      a.balance_cents AS imported_balance,
                      a.cleared_balance_cents AS imported_cleared,
                      (SELECT balance_cents
                         FROM account_balance_observed
                        WHERE account_id = a.id
                        ORDER BY as_of_date DESC LIMIT 1) AS observed
               FROM account a
               WHERE a.closed = 0 AND a.on_budget = 1
               ORDER BY a.name""",
        ).fetchall()

        # Overspent + tight
        overspent = con.execute(
            """SELECT c.name, mc.available_cents, mc.budgeted_cents
               FROM month_category mc JOIN category c ON c.id = mc.category_id
               WHERE mc.month = ? AND mc.available_cents < 0 AND c.is_spending = 1
               ORDER BY mc.available_cents ASC""",
            (month,),
        ).fetchall()
        tight = con.execute(
            """SELECT c.name, mc.available_cents, mc.budgeted_cents
               FROM month_category mc JOIN category c ON c.id = mc.category_id
               WHERE mc.month = ?
                 AND mc.available_cents >= 0
                 AND mc.budgeted_cents > 0
                 AND mc.available_cents * 4 < mc.budgeted_cents
                 AND c.is_spending = 1
               ORDER BY mc.available_cents ASC LIMIT 5""",
            (month,),
        ).fetchall()

    # Outstanding queue — items waiting for the user to categorize.
    # Surfaced at the top so the user sees "you have N items waiting"
    # right when they open the morning DM.
    with storage.connect(db_path) as con:
        outstanding = con.execute(
            "SELECT COUNT(*) FROM pending_txn WHERE status = 'pending'"
        ).fetchone()[0]
        outstanding_orders = con.execute(
            "SELECT COUNT(*) FROM pending_order WHERE status = 'pending'"
        ).fetchone()[0]

    lines: list[str] = []
    lines.append(f"☀️ {today.strftime('%A %B %d')}")
    lines.append("")

    total_outstanding = outstanding + outstanding_orders
    if total_outstanding:
        lines.append(
            f"📥 {total_outstanding} item{'s' if total_outstanding != 1 else ''} "
            f"waiting for you to categorize. Type 'next' to triage."
        )
        lines.append("")

    # Yesterday — show ALL outflows (largest first), then any inflows
    # at the bottom. The earlier version capped at 3 even when the count
    # said 6+, which made the report look truncated.
    if rows_y:
        n = len(rows_y)
        total = sum(int(r["amount_cents"]) for r in rows_y)
        outflows = sorted(
            (r for r in rows_y if int(r["amount_cents"]) < 0),
            key=lambda r: int(r["amount_cents"]),
        )
        inflows = sorted(
            (r for r in rows_y if int(r["amount_cents"]) > 0),
            key=lambda r: -int(r["amount_cents"]),
        )
        lines.append(f"📋 Yesterday: {n} transactions, net {_fmt(total)}")
        # Cap at 12 to keep the DM digestible. If there are more, show a
        # "... and N more" footer so the count never lies.
        MAX_LIST = 12
        listed = outflows[:MAX_LIST]
        for r in listed:
            amt = int(r["amount_cents"])
            lines.append(f"  {_fmt(amt):>10}  {_clean_payee(r['payee'] or '(no payee)')}")
        if inflows and len(listed) < MAX_LIST:
            for r in inflows[: MAX_LIST - len(listed)]:
                amt = int(r["amount_cents"])
                lines.append(f"  {_fmt(amt):>10}  {_clean_payee(r['payee'] or '(no payee)')}")
        hidden = n - min(n, MAX_LIST)
        if hidden > 0:
            lines.append(f"  … and {hidden} more")
    else:
        lines.append("📋 No activity yesterday")

    # Balances. Show observed (bank-email) balance if present, else the YNAB
    # imported balance with a "(YNAB)" suffix so the user knows it's not real
    # time yet. When bank-email parsers come online in Phase 1, observed will
    # become the default and the suffix goes away.
    if accts:
        lines.append("")
        lines.append("💰 Balances:")
        for a in accts:
            observed = a["observed"]
            imported = int(a["imported_balance"] or 0)
            if observed is not None:
                line = f"  {a['name'][:30]}: {_fmt(int(observed))}"
            else:
                line = f"  {a['name'][:30]}: {_fmt(imported)} (YNAB)"
            lines.append(line)

    # Reconciliation status — read from yesterday's audit_log events. When
    # the bot's daily loop calls reconcile_all_observed, each account
    # produces either reconcile_ok or reconcile_mismatch. Surface a tight
    # summary: ✅ count, ⚠ any mismatches with delta.
    with storage.connect(db_path) as con:
        recon_rows = con.execute(
            """SELECT event, details FROM audit_log
               WHERE event IN ('reconcile_ok', 'reconcile_mismatch')
                 AND date(ts) = ?""",
            (today.isoformat(),),
        ).fetchall()
    if recon_rows:
        ok_count = 0
        mismatches: list[tuple[str, int]] = []  # (account_label, delta_cents)
        for r in recon_rows:
            try:
                d = __import__("json").loads(r["details"] or "{}")
            except (ValueError, TypeError):
                continue
            if r["event"] == "reconcile_ok":
                ok_count += 1
            else:
                # Look up the account name for a friendly label
                with storage.connect(db_path) as con2:
                    arow = con2.execute(
                        "SELECT name FROM account WHERE id = ?",
                        (d.get("account_id"),),
                    ).fetchone()
                label = (arow["name"] if arow else d.get("account_id"))[:30]
                mismatches.append((label, int(d.get("delta_cents") or 0)))
        if mismatches:
            lines.append("")
            lines.append("⚠ Reconciliation mismatches:")
            for label, delta in mismatches:
                lines.append(f"  {label}: bank says {_fmt(delta)} from expected")
        if ok_count:
            lines.append("")
            lines.append(
                f"✅ Reconciled {ok_count} account{'s' if ok_count != 1 else ''} "
                f"against bank balances."
            )

    # Overspent
    if overspent:
        lines.append("")
        lines.append("🔴 Overspent this month:")
        for r in overspent[:5]:
            lines.append(f"  {r['name']}: {_fmt(int(r['available_cents']))}")

    # Tight
    if tight:
        lines.append("")
        lines.append("🟡 Running tight:")
        for r in tight:
            lines.append(
                f"  {r['name']}: {_fmt(int(r['available_cents']))} of "
                f"{_fmt(int(r['budgeted_cents']))} budgeted"
            )

    if not overspent and not tight:
        lines.append("")
        lines.append("🟢 Nothing tight or overspent.")

    return "\n".join(lines)


async def send_daily_summaries(app) -> None:
    """Loop opted-in recipients and DM the daily summary.

    `app` is the telegram Application instance (has bot_data["settings"]).
    Honors per-user quiet hours. Audits each send so it can be inspected via
    audit_log later.
    """
    settings = app.bot_data["settings"]
    db_path = settings.paths.database

    recipients = storage.list_recipients_for_period(db_path, "daily")
    if not recipients:
        log.info("daily: no recipients opted in")
        return

    text = build_daily_summary(db_path)

    # Map user_id → chat_id from settings.gmail_accounts
    user_to_chat = {acct.user_id: acct.chat_id for acct in settings.gmail_accounts}

    from datetime import datetime as _dt
    now = _dt.now()

    from bot.telegram_bot import _in_quiet_hours  # local import to dodge cycle
    sent = 0
    skipped = 0
    for r in recipients:
        user_id = r["user_id"]
        chat_id = user_to_chat.get(user_id)
        if chat_id is None:
            log.warning("daily: no chat_id for user %s", user_id)
            skipped += 1
            continue
        if _in_quiet_hours(now, r["quiet_hours"]):
            skipped += 1
            continue
        try:
            await app.bot.send_message(chat_id=chat_id, text=text)
            sent += 1
        except Exception as e:  # noqa: BLE001
            log.warning("daily: send to %s failed: %s", user_id, e)
            skipped += 1

    storage.audit(db_path, "daily_summary_sent",
                  {"sent": sent, "skipped": skipped, "recipients": len(recipients)})
    log.info("daily: sent %d, skipped %d", sent, skipped)
