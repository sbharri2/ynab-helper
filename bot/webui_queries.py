"""Read-query registry for the mobile web UI's `POST /q/{name}` route.

Each entry is ``f(db_path, **args) -> JSON-serializable`` — a straight port
of the desktop Tauri commands in ynabhelper-ui/src-tauri/src/commands.rs, so
the mobile SPA can read the same data over HTTP instead of Tauri IPC. Field
names match the TypeScript interfaces in ynabhelper-ui/src/lib/types.ts
exactly (both surfaces share one shape).
"""
from __future__ import annotations

import datetime as _dt
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
        "LEFT JOIN pending_txn pt "
        "  ON (pt.ynab_txn_id = t.ynab_txn_id "
        "      OR pt.ynab_txn_id = 'ledger:' || t.id) "
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


REGISTRY: dict[str, Callable[..., Any]] = {
    "q_categories": q_categories,
    "q_category_groups": q_category_groups,
    "q_month_categories": q_month_categories,
    "q_category_avg_activity": q_category_avg_activity,
    "q_cash_trace": q_cash_trace,
    "q_cc_balance_summary": q_cc_balance_summary,
    "q_transactions": q_transactions,
    "q_inbox": q_inbox,
}
