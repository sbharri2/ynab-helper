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


def build_daily_summary(
    db_path: str, *, user_id: str = "steven", as_of: date | None = None,
) -> str:
    """Returns the morning-summary text. No I/O beyond SQLite reads.

    The body shape depends on ``user_id``. Steven gets the full household
    snapshot (yesterday's activity, all balances, reconciliation, queues).
    Allison gets a tailored view: her personal-budget envelope, the family
    dining/entertainment envelope (MTD + yesterday's line items), and her
    own pending-queue count.
    """
    if user_id == "allison":
        return _build_allison_summary(db_path, as_of=as_of)
    return _build_steven_summary(db_path, as_of=as_of)


def _build_steven_summary(db_path: str, *, as_of: date | None = None) -> str:
    today = as_of or date.today()
    yesterday = today - timedelta(days=1)
    month = today.strftime("%Y-%m")

    with storage.connect(db_path) as con:
        # Yesterday's activity (excluding transfers — no transfer flag yet but
        # all our ledger_txn entries have no transfer_account_id concept;
        # ledger_txn already excludes those by Phase 2 design.)
        rows_y = con.execute(
            # parent_txn_id IS NULL → one row per bank transaction (split
            # children hidden); the parent carries the full amount so the
            # net total stays correct.
            """SELECT amount_cents, payee, category_id
               FROM ledger_txn
               WHERE posted_date = ? AND parent_txn_id IS NULL""",
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

        tracking = con.execute(
            """SELECT a.id, a.name, a.type, a.on_budget,
                      a.balance_cents AS imported_balance,
                      (SELECT balance_cents
                         FROM account_balance_observed
                        WHERE account_id = a.id
                        ORDER BY as_of_date DESC LIMIT 1) AS observed
               FROM account a
               WHERE a.closed = 0 AND a.on_budget = 0
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
    #
    # The /batch and /amazon queues are tracked separately. Daily
    # summary always shows both so Steven never has to remember to run
    # /amazon — the count is right there next to the /batch one.
    from bot.batch_processor import (
        count_cold_batch, count_amazon_ready, count_amazon_held,
    )
    # NOTE: settings.gmail_accounts has user_id; the daily summary is
    # currently single-user (Steven), so reading the first account's
    # user_id is correct. Multi-user fan-out lives at the loop level.
    batch_n = count_cold_batch(db_path, user_id="steven")
    amazon_ready = count_amazon_ready(db_path, user_id="steven")
    amazon_held = count_amazon_held(db_path, user_id="steven")
    with storage.connect(db_path) as con:
        # Steven's non-Amazon order rows only. The old query was household-wide
        # and unfiltered, so it counted Allison's rows plus all 30+ Amazon
        # orders — already surfaced on the 📦 Amazon line above — and reported a
        # "39 pending" that contradicted list_pending's "1 + 7" when Steven
        # asked in chat. Match list_pending's filter so the surfaces agree.
        outstanding_orders = con.execute(
            "SELECT COUNT(*) FROM pending_order "
            "WHERE status = 'pending' AND assigned_to_user_id = 'steven' "
            "AND source <> 'amazon'"
        ).fetchone()[0]

    lines: list[str] = []
    lines.append(f"☀️ {today.strftime('%A %B %d')}")
    lines.append("")

    queue_lines: list[str] = []
    if batch_n:
        # Group-era wording: the Inbox (desktop) is where the backlog
        # lives; /batch still works in a DM but isn't the headline.
        queue_lines.append(
            f"📥 {batch_n} item{'s' if batch_n != 1 else ''} in the Inbox"
        )
    if amazon_ready or amazon_held:
        parts: list[str] = []
        if amazon_ready:
            parts.append(f"{amazon_ready} ready")
        if amazon_held:
            parts.append(f"{amazon_held} waiting for receipt")
        queue_lines.append(f"📦 Amazon: {' · '.join(parts)}")
    if outstanding_orders:
        queue_lines.append(
            f"🛒 {outstanding_orders} order item{'s' if outstanding_orders != 1 else ''} pending"
        )
    if queue_lines:
        lines.extend(queue_lines)
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

    if tracking:
        lines.append("")
        lines.append("🏦 Tracking:")
        for a in tracking:
            observed = a["observed"]
            imported = int(a["imported_balance"] or 0)
            if observed is not None:
                line = f"  {a['name'][:30]}: {_fmt(int(observed))}"
            else:
                line = f"  {a['name'][:30]}: {_fmt(imported)} (YNAB)"
            lines.append(line)

    # Reconciliation status — read latest event per (account_id, as_of_date)
    # from today's audit_log. The reconciler may run multiple times per day
    # (after backfills, after manual triggers); we want the MOST RECENT
    # state per account, not every run.
    with storage.connect(db_path) as con:
        recon_rows = con.execute(
            """SELECT event, details, ts FROM audit_log
               WHERE event IN ('reconcile_ok', 'reconcile_mismatch')
                 AND date(ts) = ?
               ORDER BY id ASC""",
            (today.isoformat(),),
        ).fetchall()
    if recon_rows:
        # Dedupe to latest per account_id
        import json as _json
        latest: dict[str, dict] = {}
        for r in recon_rows:
            try:
                d = _json.loads(r["details"] or "{}")
            except (ValueError, TypeError):
                continue
            acct = d.get("account_id")
            if not acct:
                continue
            latest[acct] = {"event": r["event"], "details": d, "ts": r["ts"]}

        ok_count = 0
        mismatches: list[tuple[str, int]] = []
        for acct_id, info in latest.items():
            if info["event"] == "reconcile_ok":
                ok_count += 1
            else:
                with storage.connect(db_path) as con2:
                    arow = con2.execute(
                        "SELECT name FROM account WHERE id = ?",
                        (acct_id,),
                    ).fetchone()
                label = (arow["name"] if arow else acct_id)[:30]
                mismatches.append((label, int(info["details"].get("delta_cents") or 0)))

        if mismatches:
            lines.append("")
            lines.append("⚠ Bank balance doesn't match the ledger:")
            for label, delta in mismatches:
                # Reconciler stores delta = observed - expected. So a NEGATIVE
                # delta means bank reports LESS than the ledger (ledger over).
                direction = "under" if delta > 0 else "over"
                lines.append(
                    f"  {label}: ledger is {_fmt(abs(delta))} {direction} bank"
                )
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


# Hard-coded category names for Allison's tailored daily. Two users, two
# categories — config indirection would be more ceremony than the data
# justifies. If a third user shows up, lift these into user_pref or a
# small dict keyed on user_id.
_ALLISON_ENVELOPE_NAME = "Allison Personal Savings"
_FAMILY_DINING_NAME = "Dining Out/Entertainment"


def _lookup_category_id(con, name: str) -> str | None:
    row = con.execute(
        "SELECT id FROM category WHERE name = ? AND hidden = 0",
        (name,),
    ).fetchone()
    return row["id"] if row else None


def _build_allison_summary(db_path: str, *, as_of: date | None = None) -> str:
    """Allison's morning DM: her personal envelope + the family dining
    envelope (MTD + yesterday's line items) + her own pending queue count.

    Deliberately omits household-level balances, reconciliation status, and
    other categories' overspent/tight signals — those are Steven's lane.
    """
    today = as_of or date.today()
    yesterday = today - timedelta(days=1)
    month = today.strftime("%Y-%m")

    # Recompute the two envelopes we're about to display so the numbers
    # reflect any newly-categorized activity (the bot may have categorized
    # transactions overnight that haven't been rolled into month_category
    # yet on a cold DB).
    from bot.envelope import available_for_category

    with storage.connect(db_path) as con:
        allison_env_id = _lookup_category_id(con, _ALLISON_ENVELOPE_NAME)
        dining_id = _lookup_category_id(con, _FAMILY_DINING_NAME)

    if allison_env_id:
        available_for_category(db_path, category_id=allison_env_id, month=month)
    if dining_id:
        available_for_category(db_path, category_id=dining_id, month=month)

    with storage.connect(db_path) as con:
        allison_env = con.execute(
            "SELECT budgeted_cents, activity_cents, available_cents "
            "FROM month_category WHERE month = ? AND category_id = ?",
            (month, allison_env_id),
        ).fetchone() if allison_env_id else None

        dining_env = con.execute(
            "SELECT budgeted_cents, activity_cents, available_cents "
            "FROM month_category WHERE month = ? AND category_id = ?",
            (month, dining_id),
        ).fetchone() if dining_id else None

        # Yesterday's dining/entertainment line items. parent_txn_id IS NULL
        # so split children don't double-count; is_split=0 keeps split
        # parents out (their children carry the category).
        dining_lines = con.execute(
            """SELECT amount_cents, payee
               FROM ledger_txn
               WHERE category_id = ?
                 AND posted_date = ?
                 AND parent_txn_id IS NULL
                 AND is_split = 0
               ORDER BY amount_cents ASC""",
            (dining_id, yesterday),
        ).fetchall() if dining_id else []

        # Her pending queue — items assigned to her, still waiting on input.
        # Both pending_txn (interactive queue) and pending_order (raw email
        # rows). pending_order is on its way out per Steven's 2026-06-26
        # directive, so we don't render it as a separate line; just include
        # it in the count so she sees a non-zero number if anything is
        # genuinely waiting on her.
        pending_count = con.execute(
            "SELECT COUNT(*) FROM pending_txn "
            "WHERE assigned_to_user_id = ? AND status = 'pending'",
            ("allison",),
        ).fetchone()[0]

    lines: list[str] = []
    lines.append(f"☀️ {today.strftime('%A %B %d')}")
    lines.append("")

    if pending_count:
        lines.append(
            f"📥 {pending_count} item{'s' if pending_count != 1 else ''} "
            f"waiting for you"
        )
        lines.append("")

    # Allison's personal envelope.
    if allison_env:
        avail = int(allison_env["available_cents"] or 0)
        budgeted = int(allison_env["budgeted_cents"] or 0)
        lines.append(f"💰 {_ALLISON_ENVELOPE_NAME}")
        lines.append(f"   {_fmt(avail)} available")
        if budgeted:
            lines.append(f"   ({_fmt(budgeted)} assigned this month)")
    else:
        lines.append(f"💰 {_ALLISON_ENVELOPE_NAME}: (envelope not found)")

    # Family dining/entertainment.
    lines.append("")
    if dining_env:
        avail = int(dining_env["available_cents"] or 0)
        budgeted = int(dining_env["budgeted_cents"] or 0)
        activity = int(dining_env["activity_cents"] or 0)
        # activity is signed (negative = outflow). Render the absolute
        # spent for clarity.
        spent = -activity if activity < 0 else 0
        lines.append(f"🍽 {_FAMILY_DINING_NAME} (this month)")
        lines.append(
            f"   {_fmt(avail)} left · {_fmt(spent)} spent of "
            f"{_fmt(budgeted)} budgeted"
        )
    else:
        lines.append(f"🍽 {_FAMILY_DINING_NAME}: (envelope not found)")

    if dining_lines:
        lines.append("")
        lines.append("   Yesterday:")
        for r in dining_lines:
            amt = int(r["amount_cents"])
            lines.append(
                f"     {_fmt(amt):>10}  {_clean_payee(r['payee'] or '(no payee)')}"
            )

    return "\n".join(lines)


async def send_daily_summaries(app, *, only_user_id: str | None = None) -> None:
    """Loop opted-in recipients and DM the daily summary.

    `app` is the telegram Application instance (has bot_data["settings"]).
    Honors per-user quiet hours. Audits each send so it can be inspected via
    audit_log later.

    When ``only_user_id`` is set, only that user is DM'd — used by the
    per-user fire-time loop so each spouse gets their report at their own
    time without one user's quiet-hours suppression also blocking the other.
    """
    settings = app.bot_data["settings"]
    db_path = settings.paths.database

    # Redesign-v2 (2026-07-09): with the household group configured, ONE
    # full household summary goes to the group instead of per-person DMs.
    from bot.group_chat import report_target
    target = report_target(app)
    if target is not None:
        bot, group_id = target
        text = build_daily_summary(db_path, user_id="steven")
        try:
            await bot.send_message(chat_id=group_id, text=text)
            storage.audit(db_path, "daily_summary_sent",
                          {"sent": 1, "skipped": 0, "group": True})
        except Exception as e:  # noqa: BLE001
            log.warning("daily: group send failed: %s", e)
            storage.audit(db_path, "daily_summary_sent",
                          {"sent": 0, "skipped": 1, "group": True})
        return

    recipients = storage.list_recipients_for_period(db_path, "daily")
    if only_user_id is not None:
        recipients = [r for r in recipients if r["user_id"] == only_user_id]
    if not recipients:
        log.info("daily: no recipients opted in"
                 + (f" for user={only_user_id}" if only_user_id else ""))
        return

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
        # Body shape is per-user — Allison gets a tailored slice, everyone
        # else gets the full household summary.
        text = build_daily_summary(db_path, user_id=user_id)
        try:
            from bot.telegram_bot import _bot_for_chat
            await _bot_for_chat(app, chat_id).send_message(
                chat_id=chat_id, text=text,
            )
            sent += 1
        except Exception as e:  # noqa: BLE001
            log.warning("daily: send to %s failed: %s", user_id, e)
            skipped += 1

    storage.audit(db_path, "daily_summary_sent",
                  {"sent": sent, "skipped": skipped, "recipients": len(recipients)})
    log.info("daily: sent %d, skipped %d", sent, skipped)
