"""Read-query registry for the mobile web UI's `POST /q/{name}` route.

Each entry is ``f(db_path, **args) -> JSON-serializable`` — a straight port
of the desktop Tauri commands in ynabhelper-ui/src-tauri/src/commands.rs, so
the mobile SPA can read the same data over HTTP instead of Tauri IPC. Field
names match the TypeScript interfaces in ynabhelper-ui/src/lib/types.ts
exactly (both surfaces share one shape).
"""
from __future__ import annotations

import datetime as _dt
import math
from typing import Any, Callable

from bot import storage


def q_categories(db_path: str, **_: Any) -> list[dict[str, Any]]:
    """Every visible category — port of commands.rs:109 (`Category[]`)."""
    with storage.connect(db_path) as con:
        rows = con.execute(
            "SELECT c.id, c.group_id, g.name AS group_name, c.name, "
            "  COALESCE(c.is_spending, 0) AS is_spending, "
            "  COALESCE(c.hidden, 0) AS hidden, "
            "  c.goal_kind, c.goal_target_cents "
            "FROM category c "
            "JOIN category_group g ON g.id = c.group_id "
            "WHERE c.hidden = 0 AND g.hidden = 0 "
            "  AND g.name != 'Internal Master Category' "
            "ORDER BY g.sort_order, c.name"
        ).fetchall()
    return [dict(r) for r in rows]


def q_category_groups(db_path: str, **_: Any) -> list[dict[str, Any]]:
    """Unhidden category groups — port of commands.rs:89 (`CategoryGroup[]`)."""
    with storage.connect(db_path) as con:
        rows = con.execute(
            "SELECT id, name, COALESCE(sort_order, 0) AS sort_order "
            "FROM category_group WHERE hidden = 0 ORDER BY sort_order, name"
        ).fetchall()
    return [dict(r) for r in rows]


def q_month_categories(db_path: str, month: str, **_: Any) -> list[dict[str, Any]]:
    """Per-category budget/activity/available for one month —
    port of commands.rs:143 (`MonthCategoryRow[]`)."""
    with storage.connect(db_path) as con:
        rows = con.execute(
            "SELECT mc.month, mc.category_id, c.name AS category_name, "
            "  g.name AS group_name, "
            "  COALESCE(mc.budgeted_cents, 0) AS budgeted_cents, "
            "  COALESCE(mc.activity_cents, 0) AS activity_cents, "
            "  COALESCE(mc.available_cents, 0) AS available_cents "
            "FROM month_category mc "
            "JOIN category c ON c.id = mc.category_id "
            "JOIN category_group g ON g.id = c.group_id "
            "WHERE mc.month = ? "
            "  AND c.hidden = 0 AND g.hidden = 0 "
            "  AND g.name != 'Internal Master Category' "
            "  AND g.name != 'Investments / Savings Transfers' "
            "ORDER BY g.sort_order, c.name",
            (month,),
        ).fetchall()
    return [dict(r) for r in rows]


def q_category_avg_activity(
    db_path: str,
    months: int | None = None,
    include_reimbursables: bool | None = None,
    **_: Any,
) -> list[dict[str, Any]]:
    """Trailing-N-month average outflow per category (skips the partial
    current month) — port of commands.rs:2686 (`CategoryAvgRow[]`)."""
    months = max(int(months) if months is not None else 6, 1)
    reimb_clause = (
        "" if include_reimbursables else
        "AND LOWER(c.name) NOT LIKE '%reimbursabl%' "
        "AND LOWER(g.name) NOT LIKE '%reimbursabl%' "
    )
    sql = (
        "WITH window_txns AS ("
        "    SELECT lt.category_id, "
        "           strftime('%Y-%m', lt.posted_date) AS m, "
        "           -lt.amount_cents AS out_cents "
        "    FROM ledger_txn lt "
        "    JOIN category c ON c.id = lt.category_id "
        "    JOIN category_group g ON g.id = c.group_id "
        "    WHERE lt.posted_date >= date('now', 'start of month', ?) "
        "      AND lt.posted_date <  date('now', 'start of month') "
        "      AND lt.amount_cents < 0 "
        "      AND (lt.payee IS NULL OR lt.payee NOT LIKE 'Transfer :%') "
        "      AND lt.transfer_account_id IS NULL "
        "      AND lt.account_id IN (SELECT id FROM account WHERE on_budget = 1) "
        f"     {reimb_clause}"
        "), "
        "monthly AS ("
        "    SELECT category_id, m, SUM(out_cents) AS month_total "
        "    FROM window_txns "
        "    GROUP BY category_id, m"
        ") "
        "SELECT category_id, "
        "       COUNT(DISTINCT m) AS months_observed, "
        "       CAST(SUM(month_total) / CAST(? AS REAL) AS INTEGER) AS avg_cents "
        "FROM monthly GROUP BY category_id"
    )
    window_arg = f"-{months} months"
    with storage.connect(db_path) as con:
        rows = con.execute(sql, (window_arg, months)).fetchall()
    return [dict(r) for r in rows]


def q_cash_trace(db_path: str, months: int, **_: Any) -> list[dict[str, Any]]:
    """Month-by-month cash walk for the on-budget cash accounts —
    port of commands.rs:2496 (`CashTraceRow[]`). Months are clamped to
    1..=24, matching the Rust `.clamp(1, 24)`."""
    months = max(1, min(int(months), 24))
    today = _dt.date.today()

    y = today.year
    m = today.month - (months - 1)
    while m < 1:
        m += 12
        y -= 1
    window_start = f"{y:04d}-{m:02d}-01"

    with storage.connect(db_path) as con:
        rolling_row = con.execute(
            "SELECT COALESCE(SUM(lt.amount_cents), 0) "
            "FROM ledger_txn lt "
            "JOIN account a ON a.id = lt.account_id "
            "WHERE a.on_budget = 1 AND a.closed = 0 "
            "  AND lt.is_split = 0 AND lt.posted_date < ?",
            (window_start,),
        ).fetchone()
        rolling = rolling_row[0] if rolling_row and rolling_row[0] is not None else 0

        by_month_rows = con.execute(
            "SELECT strftime('%Y-%m', lt.posted_date) AS mo, "
            "  COALESCE(SUM(CASE WHEN lt.amount_cents > 0 "
            "                    THEN lt.amount_cents ELSE 0 END), 0) AS inflow, "
            "  COALESCE(SUM(CASE WHEN lt.amount_cents < 0 "
            "                    THEN lt.amount_cents ELSE 0 END), 0) AS outflow "
            "FROM ledger_txn lt "
            "JOIN account a ON a.id = lt.account_id "
            "WHERE a.on_budget = 1 AND a.closed = 0 "
            "  AND lt.is_split = 0 AND lt.posted_date >= ? "
            "GROUP BY mo ORDER BY mo",
            (window_start,),
        ).fetchall()
    by_month = {r["mo"]: (r["inflow"], r["outflow"]) for r in by_month_rows}

    rows: list[dict[str, Any]] = []
    yy, mm = y, m
    for _i in range(months):
        key = f"{yy:04d}-{mm:02d}"
        inflow, outflow = by_month.get(key, (0, 0))
        net = inflow + outflow
        rolling += net
        rows.append({
            "month": key,
            "inflow_cents": inflow,
            "outflow_cents": outflow,
            "net_cents": net,
            "end_cash_cents": rolling,
        })
        mm += 1
        if mm > 12:
            mm = 1
            yy += 1
    return rows


def q_cc_balance_summary(db_path: str, **_: Any) -> dict[str, Any]:
    """Cross-account CC + checking summary for the Budget page header —
    port of commands.rs:2575 (`CcSummary`). CC balance_cents is negative
    when you owe (YNAB convention); cc_total_owed_cents flips the sign
    so the UI shows "you owe $X" as a positive number."""
    with storage.connect(db_path) as con:
        card_rows = con.execute(
            "SELECT id AS account_id, name, "
            "  COALESCE(balance_cents, 0) AS balance_cents "
            "FROM account "
            "WHERE closed = 0 AND type = 'credit_card' "
            "ORDER BY name"
        ).fetchall()
        checking_row = con.execute(
            "SELECT COALESCE(SUM(balance_cents), 0) "
            "FROM account "
            "WHERE closed = 0 "
            "  AND type IN ('checking', 'savings') "
            "  AND on_budget = 1"
        ).fetchone()
    cards = [dict(r) for r in card_rows]
    checking_total_cents = checking_row[0] if checking_row and checking_row[0] is not None else 0
    cc_total_owed_cents = -sum(c["balance_cents"] for c in cards)
    after_payoff_cents = checking_total_cents - cc_total_owed_cents
    return {
        "cc_total_owed_cents": cc_total_owed_cents,
        "checking_total_cents": checking_total_cents,
        "after_payoff_cents": after_payoff_cents,
        "cards": cards,
    }


def q_transactions(
    db_path: str,
    account_id: str | None = None,
    category_id: str | None = None,
    payee_search: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
    **_: Any,
) -> list[dict[str, Any]]:
    """Register/inbox-aware transaction list — port of commands.rs:192
    (`Transaction[]`). When filtering by category, split CHILD legs
    carrying that category are shown (is_split=0, matched via
    category_id); with no category filter, splits collapse to one bank
    row per transaction (parent_txn_id IS NULL)."""
    has_category_filter = category_id is not None
    sql = (
        "SELECT t.id, t.account_id, a.name AS account_name, t.posted_date, "
        "  t.amount_cents, t.payee, t.memo, t.category_id, "
        "  c.name AS category_name, "
        "  COALESCE(t.cleared, 'uncleared') AS cleared, "
        "  t.transfer_account_id, ta.name AS transfer_account_name, "
        "  pt.id AS pending_id, pt.status AS decision_state, "
        "  COALESCE(pt.raw_summary, pt.memo) AS evidence_summary, "
        "  pt.suggested_category AS suggested_category_id, "
        "  suggested.name AS suggested_category_name, "
        "  EXISTS(SELECT 1 FROM question q "
        "         WHERE q.item_kind='txn' AND q.item_id=pt.id "
        "           AND q.state='open') AS asked_in_chat, "
        "  a.on_budget AS account_on_budget, t.is_split AS is_split "
        "FROM ledger_txn t "
        "JOIN account a ON a.id = t.account_id "
        "LEFT JOIN category c ON c.id = t.category_id "
        "LEFT JOIN account ta ON ta.id = t.transfer_account_id "
        # Exactly ONE pending row per txn: a ledger row can be claimed
        # by two pts under the two key forms (a coastal 'ledger:<id>' pt
        # AND a YNAB-uuid pt for the same charge — the nail-spa dupe,
        # 2026-07-24). Prefer the open one, newest wins.
        "LEFT JOIN pending_txn pt ON pt.id = ("
        "  SELECT p2.id FROM pending_txn p2 "
        "  WHERE p2.ynab_txn_id = t.ynab_txn_id "
        "     OR p2.ynab_txn_id = 'ledger:' || t.id "
        "  ORDER BY CASE WHEN p2.status IN ('pending','skipped') "
        "           THEN 0 ELSE 1 END, p2.id DESC LIMIT 1) "
        "LEFT JOIN category suggested ON suggested.id = pt.suggested_category "
        "WHERE 1=1 "
    )
    params: list[Any] = []
    if account_id is not None:
        sql += " AND t.account_id = ? "
        params.append(account_id)
    if category_id is not None:
        sql += " AND t.category_id = ? "
        params.append(category_id)
    if payee_search is not None:
        sql += " AND LOWER(t.payee) LIKE ? "
        params.append(f"%{payee_search.lower()}%")
    if since is not None:
        sql += " AND t.posted_date >= ? "
        params.append(since)
    if until is not None:
        sql += " AND t.posted_date <= ? "
        params.append(until)
    if not has_category_filter:
        sql += " AND t.parent_txn_id IS NULL "
    sql += " ORDER BY t.posted_date DESC, t.id DESC "
    # limit/offset are ints, never user-controlled SQL — but they arrive
    # here as arbitrary **kwargs (e.g. from JSON over HTTP), so coerce
    # with int() before interpolating them into the query text.
    sql += (
        f" LIMIT {int(limit) if limit is not None else 500} "
        f"OFFSET {int(offset) if offset is not None else 0} "
    )

    with storage.connect(db_path) as con:
        rows = con.execute(sql, params).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        d["asked_in_chat"] = bool(d["asked_in_chat"])
        d["account_on_budget"] = bool(d["account_on_budget"])
        d["is_split"] = bool(d["is_split"])
        out.append(d)
    return out


def q_inbox(db_path: str, **_: Any) -> list[dict[str, Any]]:
    """Needs-review queue for the Inbox panel — port of commands.rs:2864
    (`InboxRow[]`)."""
    with storage.connect(db_path) as con:
        rows = con.execute(
            "SELECT pt.id, pt.txn_date, pt.payee, "
            "  COALESCE(pt.amount_cents, 0) AS amount_cents, "
            "  pt.memo, pt.raw_summary, pt.status, "
            "  pt.queue_lane, pt.assigned_to_user_id, "
            "  pt.suggested_category AS suggested_category_id, "
            "  c.name AS suggested_category_name "
            "FROM pending_txn pt "
            "LEFT JOIN category c ON c.id = pt.suggested_category "
            "WHERE pt.status IN ('pending', 'skipped') "
            "ORDER BY pt.txn_date DESC, pt.id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def _is_calendar_semi_monthly(dates: list[_dt.date]) -> bool:
    """Port of commands.rs:1916 `is_calendar_semi_monthly` — distinguishes
    calendar-anchored semi-monthly pay (~15th + ~end of month) from true
    biweekly (every 14 days, drifts across the calendar) when both produce
    similar median gaps. Requires >=6 dates (>=5 gaps) for stability."""
    if len(dates) < 6:
        return False
    has_early = any(d.day <= 18 for d in dates)
    has_late = any(d.day >= 17 for d in dates)
    if not (has_early and has_late):
        return False
    gaps = sorted((dates[i] - dates[i - 1]).days for i in range(1, len(dates)))
    gaps.pop()  # drop the single largest gap (weekend/holiday shift)
    if not gaps:
        return False
    spread = max(gaps) - min(gaps)
    return spread >= 3


def _next_predicted_date(
    prev: _dt.date, cadence: str, median_delta_days: int,
) -> _dt.date:
    """Port of commands.rs:1944 `next_predicted_date`. Advances `prev` by
    one cycle of `cadence`: semi-monthly jumps to the opposite calendar
    half (29th / 15th, clamped to month-end), monthly keeps the same
    day-of-month (clamped to month-end), everything else falls back to
    +median_delta_days."""
    if cadence == "semi-monthly":
        day = prev.day
        if day <= 18:
            y, m = prev.year, prev.month
            try:
                return _dt.date(y, m, 29)
            except ValueError:
                next_first = (
                    _dt.date(y + 1, 1, 1) if m == 12
                    else _dt.date(y, m + 1, 1)
                )
                return next_first - _dt.timedelta(days=1)
        else:
            y, m = (prev.year + 1, 1) if prev.month == 12 else (prev.year, prev.month + 1)
            return _dt.date(y, m, 15)
    elif cadence == "monthly":
        y, m = (prev.year + 1, 1) if prev.month == 12 else (prev.year, prev.month + 1)
        day = prev.day
        try:
            return _dt.date(y, m, day)
        except ValueError:
            next_first = (
                _dt.date(y + 1, 1, 1) if m == 12
                else _dt.date(y, m + 1, 1)
            )
            return next_first - _dt.timedelta(days=1)
    else:
        return prev + _dt.timedelta(days=median_delta_days)


def q_income_sources(db_path: str, **_: Any) -> list[dict[str, Any]]:
    """Auto-detect recurring income sources from the last 6 months of
    inflows, then merge in `income_source_override` rows — port of
    commands.rs:2018 (`IncomeSource[]`). See the Rust docstring for the
    full algorithm (6-month grouping -> cv/median/cadence filter -> 5%
    dedupe -> manual override merge with retired-key filtering)."""
    with storage.connect(db_path) as con:
        rows = con.execute(
            "SELECT "
            "  UPPER(TRIM(COALESCE(payee, '(no payee)'))) AS payee_key, "
            "  COALESCE(payee, '(no payee)') AS display, "
            "  COUNT(*) AS n, "
            "  MIN(posted_date) AS first_seen, "
            "  MAX(posted_date) AS last_seen, "
            "  GROUP_CONCAT(amount_cents, ',') AS amounts, "
            "  GROUP_CONCAT(posted_date, ',') AS dates "
            "FROM ledger_txn "
            "WHERE amount_cents > 0 "
            "  AND posted_date >= date('now', '-6 months') "
            "  AND parent_txn_id IS NULL "
            "  AND (payee IS NULL OR payee NOT LIKE 'Transfer :%') "
            "  AND transfer_account_id IS NULL "
            "  AND account_id IN (SELECT id FROM account WHERE on_budget = 1) "
            "GROUP BY payee_key "
            "HAVING n >= 3"
        ).fetchall()
        override_rows = con.execute(
            "SELECT payee_key, status, display_name, expected_amount_cents, "
            "  cadence, first_expected_date, median_delta_days "
            "FROM income_source_override"
        ).fetchall()

    candidates: list[dict[str, Any]] = []
    for r in rows:
        amounts = sorted(
            int(x) for x in (r["amounts"] or "").split(",") if x != ""
        )
        if not amounts:
            continue
        median = amounts[len(amounts) // 2]
        if median < 50_000:  # skip < $500
            continue
        mean = sum(amounts) / len(amounts)
        variance = sum((a - mean) ** 2 for a in amounts) / len(amounts)
        cv = (math.sqrt(variance) / mean) if mean > 0 else 1.0
        if cv > 0.20:
            continue
        last_amount = amounts[-1]  # NB: max of amounts, not most-recent by
                                    # date — this mislabeling is in the Rust
                                    # source too; ported as-is.

        dates: list[_dt.date] = []
        for s in (r["dates"] or "").split(","):
            try:
                dates.append(_dt.datetime.strptime(s, "%Y-%m-%d").date())
            except ValueError:
                continue
        dates.sort()
        deltas = sorted(
            (dates[i] - dates[i - 1]).days for i in range(1, len(dates))
        )
        median_delta = deltas[len(deltas) // 2] if deltas else 30

        if 13 <= median_delta <= 15:
            cadence = (
                "semi-monthly" if _is_calendar_semi_monthly(dates)
                else "biweekly"
            )
        elif 16 <= median_delta <= 17:
            cadence = "semi-monthly"
        elif 28 <= median_delta <= 32:
            cadence = "monthly"
        else:
            continue

        candidates.append({
            "key": r["payee_key"],
            "display": r["display"],
            "median": median,
            "last_amount": last_amount,
            "n": r["n"],
            "cadence": cadence,
            "median_delta": median_delta,
            "last_seen": r["last_seen"],
        })

    # Dedupe: walk most-recent-first, drop anything within 5% of a median
    # already kept (handles the CC-alert-vs-YNAB-payee duplication).
    candidates.sort(key=lambda c: c["last_seen"], reverse=True)
    kept: list[dict[str, Any]] = []
    for cand in candidates:
        is_dup = any(
            abs(cand["median"] - k["median"]) / max(k["median"], 1) < 0.05
            for k in kept
        )
        if not is_dup:
            kept.append(cand)

    today = _dt.date.today()
    out: list[dict[str, Any]] = []
    for c in kept:
        try:
            last = _dt.datetime.strptime(c["last_seen"], "%Y-%m-%d").date()
        except ValueError:
            last = today
        expected_next = last + _dt.timedelta(days=c["median_delta"])
        out.append({
            "payee_key": c["key"],
            "display_payee": c["display"],
            "median_cents": c["median"],
            "occurrences": c["n"],
            "cadence": c["cadence"],
            "median_delta_days": c["median_delta"],
            "last_seen": c["last_seen"],
            "last_amount_cents": c["last_amount"],
            "expected_next": expected_next.strftime("%Y-%m-%d"),
        })

    # Apply manual overrides: filter retired auto-detected keys, append
    # active manual entries not already auto-detected.
    retired: set[str] = set()
    manual_active: list[dict[str, Any]] = []
    for r in override_rows:
        key = r["payee_key"]
        status = r["status"]
        if status == "retired":
            retired.add(key)
        elif status == "active":
            if any(s["payee_key"] == key for s in out):
                continue
            display = r["display_name"]
            display_payee = display if display is not None else key
            median = (
                r["expected_amount_cents"]
                if r["expected_amount_cents"] is not None else 0
            )
            median_delta = (
                r["median_delta_days"]
                if r["median_delta_days"] is not None else 14
            )
            first_date = r["first_expected_date"]
            # posted/DATE columns come back as `date` objects via the
            # storage layer's PARSE_DECLTYPES converter, not strings.
            first_date_s = (
                first_date.strftime("%Y-%m-%d")
                if isinstance(first_date, _dt.date) else first_date
            )
            last_seen = (
                first_date_s if first_date_s is not None
                else today.strftime("%Y-%m-%d")
            )
            expected_next = first_date_s if first_date_s is not None else last_seen
            cadence = r["cadence"] if r["cadence"] is not None else "monthly"
            manual_active.append({
                "payee_key": key,
                "display_payee": display_payee,
                "median_cents": median,
                "occurrences": 0,
                "cadence": cadence,
                "median_delta_days": median_delta,
                "last_seen": last_seen,
                "last_amount_cents": median,
                "expected_next": expected_next,
            })

    out = [s for s in out if s["payee_key"] not in retired]
    out.extend(manual_active)
    return out


def q_ready_to_assign(db_path: str, month: str, **_: Any) -> dict[str, Any]:
    """Ready-to-Assign for one month — port of commands.rs:2237
    (`ReadyToAssign`). Predicts each detected income source's hits in the
    month, matches actual inflows within +/-3 days, surfaces unmatched
    in-month actuals for a known source as "received" rows (2026-07-24
    fix), sums misc/assigned, and computes the cash-anchored RTA plus the
    in-flight transfer-leg detector."""
    sources = q_income_sources(db_path)
    today = _dt.date.today()
    month_start = _dt.datetime.strptime(f"{month}-01", "%Y-%m-%d").date()
    if month_start.month == 12:
        month_end = _dt.date(month_start.year + 1, 1, 1)
    else:
        month_end = _dt.date(month_start.year, month_start.month + 1, 1)
    month_start_s = month_start.strftime("%Y-%m-%d")
    month_end_s = month_end.strftime("%Y-%m-%d")

    with storage.connect(db_path) as con:
        actual_rows = con.execute(
            "SELECT "
            "  UPPER(TRIM(COALESCE(payee, '(no payee)'))) AS payee_key, "
            "  posted_date, amount_cents "
            "FROM ledger_txn "
            "WHERE amount_cents > 0 "
            "  AND posted_date >= ? AND posted_date < ? "
            "  AND parent_txn_id IS NULL "
            "  AND (payee IS NULL OR payee NOT LIKE 'Transfer :%') "
            "  AND transfer_account_id IS NULL "
            "  AND account_id IN (SELECT id FROM account WHERE on_budget = 1)",
            (month_start_s, month_end_s),
        ).fetchall()

        assigned_row = con.execute(
            "SELECT COALESCE(SUM(mc.budgeted_cents), 0) "
            "FROM month_category mc "
            "JOIN category c ON c.id = mc.category_id "
            "JOIN category_group g ON g.id = c.group_id "
            "WHERE mc.month = ? AND g.name != 'Internal Master Category'",
            (month,),
        ).fetchone()
        assigned_cents = assigned_row[0] if assigned_row[0] is not None else 0

        cash_row = con.execute(
            "SELECT COALESCE(SUM(lt.amount_cents), 0) "
            "FROM ledger_txn lt "
            "JOIN account a ON a.id = lt.account_id "
            "WHERE a.on_budget = 1 AND a.closed = 0 AND lt.is_split = 0"
        ).fetchone()
        cash_cents = cash_row[0] if cash_row[0] is not None else 0

        available_row = con.execute(
            "SELECT COALESCE(SUM(mc.available_cents), 0) "
            "FROM month_category mc "
            "JOIN category c ON c.id = mc.category_id "
            "JOIN category_group g ON g.id = c.group_id "
            "WHERE mc.month = ? AND g.name != 'Internal Master Category'",
            (month,),
        ).fetchone()
        available_cents = available_row[0] if available_row[0] is not None else 0

        in_flight_row = con.execute(
            "SELECT COALESCE(SUM(-lt.amount_cents), 0) "
            "FROM ledger_txn lt "
            "JOIN account a  ON a.id  = lt.account_id "
            "JOIN account a2 ON a2.id = lt.transfer_account_id "
            "WHERE a.on_budget = 1 AND a.closed = 0 "
            "  AND a2.on_budget = 1 AND a2.closed = 0 "
            "  AND lt.is_split = 0 "
            "  AND lt.posted_date >= date('now', '-10 days') "
            "  AND NOT EXISTS ( "
            "    SELECT 1 FROM ledger_txn m "
            "    WHERE m.account_id = lt.transfer_account_id "
            "      AND m.amount_cents = -lt.amount_cents "
            "      AND m.is_split = 0 "
            "      AND ABS(julianday(m.posted_date) - julianday(lt.posted_date)) <= 5 "
            "  )"
        ).fetchone()
        in_flight_cents = in_flight_row[0] if in_flight_row[0] is not None else 0

    actuals_by_key: dict[str, list[tuple[_dt.date, int]]] = {}
    for r in actual_rows:
        d = r["posted_date"]
        if isinstance(d, str):
            try:
                d = _dt.datetime.strptime(d, "%Y-%m-%d").date()
            except ValueError:
                continue
        elif not isinstance(d, _dt.date):
            continue
        actuals_by_key.setdefault(r["payee_key"], []).append((d, r["amount_cents"]))

    paychecks: list[dict[str, Any]] = []
    any_overdue = False
    total_paycheck_cents = 0
    matched_actuals: set[tuple[str, str]] = set()

    for src in sources:
        src_rows_start = len(paychecks)
        cadence = src["cadence"]
        median_delta_days = src["median_delta_days"]
        try:
            last = _dt.datetime.strptime(src["last_seen"], "%Y-%m-%d").date()
        except ValueError:
            last = today
        cursor = last

        # Walk forward until we land at/past the month's start.
        while True:
            nxt = _next_predicted_date(cursor, cadence, median_delta_days)
            if nxt >= month_start:
                break
            cursor = nxt

        # Step forward through the month, recording predicted hits.
        while True:
            cursor = _next_predicted_date(cursor, cadence, median_delta_days)
            if cursor >= month_end:
                break
            if cursor < month_start:
                continue

            actuals = actuals_by_key.get(src["payee_key"])
            actual_match = None
            if actuals:
                for d, c in actuals:
                    if abs((d - cursor).days) <= 3:
                        actual_match = (d, c)
                        break

            if actual_match is not None:
                d, c = actual_match
                matched_actuals.add((src["payee_key"], d.strftime("%Y-%m-%d")))
                actual_date: str | None = d.strftime("%Y-%m-%d")
                actual_cents: int | None = c
                status = "received"
            elif cursor > today:
                actual_date, actual_cents, status = None, None, "expected"
            else:
                any_overdue = True
                actual_date, actual_cents, status = None, None, "overdue"

            effective_cents = (
                actual_cents if actual_cents is not None else src["median_cents"]
            )
            total_paycheck_cents += effective_cents
            paychecks.append({
                "source_payee": src["display_payee"],
                "expected_date": cursor.strftime("%Y-%m-%d"),
                "expected_cents": src["median_cents"],
                "actual_date": actual_date,
                "actual_cents": actual_cents,
                "status": status,
            })

        # A detected source's walk anchors at last_seen and only steps
        # forward, so its own most recent deposit never gets a row on its
        # own — surface every unmatched in-month actual for a known
        # source as a received paycheck (2026-07-24 fix).
        actuals = actuals_by_key.get(src["payee_key"])
        if actuals:
            for d, c in actuals:
                date_s = d.strftime("%Y-%m-%d")
                k = (src["payee_key"], date_s)
                if k not in matched_actuals:
                    matched_actuals.add(k)
                    total_paycheck_cents += c
                    paychecks.append({
                        "source_payee": src["display_payee"],
                        "expected_date": date_s,
                        "expected_cents": src["median_cents"],
                        "actual_date": date_s,
                        "actual_cents": c,
                        "status": "received",
                    })

        paychecks[src_rows_start:] = sorted(
            paychecks[src_rows_start:], key=lambda p: p["expected_date"]
        )

    # Misc = actual inflows not matched to any paycheck prediction.
    misc_cents = 0
    for key, hits in actuals_by_key.items():
        for d, c in hits:
            date_s = d.strftime("%Y-%m-%d")
            if (key, date_s) not in matched_actuals:
                misc_cents += c

    # Cash-anchored RTA: on-budget cash minus everything sitting in this
    # month's envelopes (Steven, 2026-07-11 — every month rolls over).
    rta = cash_cents - available_cents

    return {
        "month": month,
        "expected_paycheck_cents": total_paycheck_cents,
        "actual_misc_cents": misc_cents,
        "assigned_cents": assigned_cents,
        "ready_to_assign_cents": rta,
        "monthly_net_cents": total_paycheck_cents + misc_cents - assigned_cents,
        "cash_cents": cash_cents,
        "available_cents": available_cents,
        "paychecks": paychecks,
        "any_overdue": any_overdue,
        "in_flight_cents": in_flight_cents,
    }


def q_seasonal_funds(db_path: str, **_: Any) -> list[dict[str, Any]]:
    """Stub — deliberate MVP degradation (2026-07-24, Task 5).

    The Seasonal panel (sinking-fund envelopes like holiday/travel) has no
    mobile-web port yet; it isn't reachable from the mobile nav. Returning
    an empty list keeps `/q/q_seasonal_funds` callable (and the desktop-
    shared Budget page's optional seasonal section tolerant of no data)
    without porting the full desktop commands.rs logic in this pass.
    """
    return []


REGISTRY: dict[str, Callable[..., Any]] = {
    "q_categories": q_categories,
    "q_category_groups": q_category_groups,
    "q_month_categories": q_month_categories,
    "q_category_avg_activity": q_category_avg_activity,
    "q_cash_trace": q_cash_trace,
    "q_cc_balance_summary": q_cc_balance_summary,
    "q_transactions": q_transactions,
    "q_inbox": q_inbox,
    "q_income_sources": q_income_sources,
    "q_ready_to_assign": q_ready_to_assign,
    "q_seasonal_funds": q_seasonal_funds,
}
