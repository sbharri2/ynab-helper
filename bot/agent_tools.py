"""Python implementations of the tools the qwen3:32b agent (bot/agent.py) can
invoke from user-facing Telegram messages.

Each function:
  - Takes typed kwargs validated by the TOOLS schema in bot/agent.py
  - Returns a human-readable string the bot DMs back to the user
  - Resolves user-supplied names (category, account) fuzzy-but-strict —
    case-insensitive contains match, ambiguous matches raise a polite refusal
  - Reads/writes via bot.storage and bot.envelope; never calls Ollama itself
    (the agent loop already had its turn)

When new capabilities are added, the pattern is: write the function here,
register the TOOL schema in bot/agent.py, and you're done.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Any

from bot import envelope, storage

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Name resolution helpers
# ---------------------------------------------------------------------------

def _resolve_category(db_path: str, name: str) -> dict | None:
    """Case-insensitive substring match against all categories (including
    non-spending — user might want to query goals too).

    Returns the category row, or None if 0 / multiple matches.
    """
    norm = (name or "").strip().lower()
    if not norm:
        return None
    with storage.connect(db_path) as con:
        rows = con.execute(
            """SELECT c.id, c.name, c.group_id, g.name AS group_name,
                      c.is_spending, c.goal_kind, c.goal_target_cents
               FROM category c
               JOIN category_group g ON g.id = c.group_id
               WHERE c.hidden = 0 AND LOWER(c.name) LIKE ?""",
            (f"%{norm}%",),
        ).fetchall()
    if len(rows) == 1:
        return dict(rows[0])
    # If multiple, prefer exact match
    for r in rows:
        if r["name"].lower() == norm:
            return dict(r)
    return None


def _resolve_account(db_path: str, name: str) -> dict | None:
    norm = (name or "").strip().lower()
    if not norm:
        return None
    with storage.connect(db_path) as con:
        rows = con.execute(
            """SELECT * FROM account
               WHERE closed = 0 AND LOWER(name) LIKE ?""",
            (f"%{norm}%",),
        ).fetchall()
    if len(rows) == 1:
        return dict(rows[0])
    for r in rows:
        if r["name"].lower() == norm:
            return dict(r)
    return None


def _current_month() -> str:
    return date.today().strftime("%Y-%m")


def _fmt_money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


# ---------------------------------------------------------------------------
# Read-side tools
# ---------------------------------------------------------------------------

def get_category_available(db_path: str, *, category_name: str) -> str:
    """How much is available in <category> this month?"""
    cat = _resolve_category(db_path, category_name)
    if cat is None:
        return f"I couldn't find a category matching '{category_name}'. Try /samples or be more specific."
    month = _current_month()
    with storage.connect(db_path) as con:
        row = con.execute(
            """SELECT budgeted_cents, activity_cents, available_cents
               FROM month_category WHERE month = ? AND category_id = ?""",
            (month, cat["id"]),
        ).fetchone()
    if row is None:
        return (
            f"{cat['name']} hasn't been touched in {month} yet — "
            f"no budget assigned, no spending recorded."
        )
    budgeted = int(row["budgeted_cents"])
    activity = int(row["activity_cents"])
    available = int(row["available_cents"])
    spent = -activity if activity < 0 else 0
    pct = (spent / budgeted * 100) if budgeted else 0
    return (
        f"{cat['name']}: {_fmt_money(available)} available\n"
        f"  Budgeted: {_fmt_money(budgeted)}\n"
        f"  Spent:    {_fmt_money(-spent)}"
        + (f"  ({pct:.0f}% of budget)" if budgeted else "")
    )


def get_account_balance(db_path: str, *, account_name: str) -> str:
    """Current balance of <account> based on ledger_txn."""
    acct = _resolve_account(db_path, account_name)
    if acct is None:
        return f"I couldn't find an account matching '{account_name}'."
    with storage.connect(db_path) as con:
        row = con.execute(
            """SELECT
                 COALESCE(SUM(amount_cents), 0) AS total,
                 COALESCE(SUM(CASE WHEN cleared IN ('cleared','reconciled') THEN amount_cents ELSE 0 END), 0) AS cleared_total
               FROM ledger_txn WHERE account_id = ?""",
            (acct["id"],),
        ).fetchone()
    cleared = int(row["cleared_total"])
    total = int(row["total"])
    pending = total - cleared
    msg = f"{acct['name']}: {_fmt_money(cleared)} cleared"
    if pending:
        msg += f", {_fmt_money(pending)} pending → {_fmt_money(total)} total"
    return msg


def list_categories_overview(db_path: str, *, group_filter: str | None = None) -> str:
    """List categories with their current month's available balance.

    If group_filter is given, only show that category group.
    """
    month = _current_month()
    with storage.connect(db_path) as con:
        q = """SELECT c.name, g.name AS group_name,
                      COALESCE(mc.available_cents, 0) AS avail,
                      COALESCE(mc.budgeted_cents, 0) AS bud
               FROM category c
               JOIN category_group g ON g.id = c.group_id
               LEFT JOIN month_category mc
                 ON mc.category_id = c.id AND mc.month = ?
               WHERE c.hidden = 0"""
        params: list[Any] = [month]
        if group_filter:
            q += " AND LOWER(g.name) LIKE ?"
            params.append(f"%{group_filter.lower()}%")
        q += " ORDER BY g.name, c.name"
        rows = con.execute(q, params).fetchall()
    if not rows:
        return "No categories matched."
    lines = []
    current_group = None
    for r in rows:
        if r["group_name"] != current_group:
            lines.append(f"\n*{r['group_name']}*")
            current_group = r["group_name"]
        lines.append(f"  {r['name']}: {_fmt_money(r['avail'])} avail")
    return "\n".join(lines).strip()


def show_drill_down(
    db_path: str,
    *,
    target: str,
    kind: str = "category",
    days: int = 30,
    limit: int = 10,
) -> str:
    """Show recent transactions for a category / payee / account.

    `kind` is one of: 'category', 'payee', 'account'.
    """
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    with storage.connect(db_path) as con:
        if kind == "category":
            cat = _resolve_category(db_path, target)
            if cat is None:
                return f"No category matched '{target}'."
            rows = con.execute(
                """SELECT posted_date, payee, amount_cents, memo
                   FROM ledger_txn
                   WHERE category_id = ? AND posted_date >= ?
                   ORDER BY posted_date DESC LIMIT ?""",
                (cat["id"], cutoff, limit),
            ).fetchall()
            header = f"{cat['name']} — last {days}d"
        elif kind == "payee":
            rows = con.execute(
                """SELECT posted_date, payee, amount_cents, memo, category_id
                   FROM ledger_txn
                   WHERE LOWER(payee) LIKE ? AND posted_date >= ?
                   ORDER BY posted_date DESC LIMIT ?""",
                (f"%{target.lower()}%", cutoff, limit),
            ).fetchall()
            header = f"{target} — last {days}d"
        elif kind == "account":
            acct = _resolve_account(db_path, target)
            if acct is None:
                return f"No account matched '{target}'."
            rows = con.execute(
                """SELECT posted_date, payee, amount_cents, memo
                   FROM ledger_txn
                   WHERE account_id = ? AND posted_date >= ?
                   ORDER BY posted_date DESC LIMIT ?""",
                (acct["id"], cutoff, limit),
            ).fetchall()
            header = f"{acct['name']} — last {days}d"
        else:
            return f"Invalid drill-down kind: {kind}"

    if not rows:
        return f"{header}: no transactions."
    total = sum(int(r["amount_cents"]) for r in rows)
    lines = [f"{header} ({len(rows)} txns, total {_fmt_money(total)}):"]
    for r in rows:
        d = r["posted_date"]
        if hasattr(d, "month"):
            # Windows strftime doesn't support %-m / %-d — use object access.
            date_str = f"{d.month}/{d.day}"
        else:
            date_str = str(d)[5:10]
        memo = (r["memo"] or "").strip()
        line = f"  {date_str}  {_fmt_money(int(r['amount_cents'])):>10}  {r['payee'] or '?'}"
        if memo:
            line += f" — {memo[:40]}"
        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Write-side tools (envelope math)
# ---------------------------------------------------------------------------

def assign_to_category_tool(
    db_path: str, *, category_name: str, amount_dollars: float,
) -> str:
    cat = _resolve_category(db_path, category_name)
    if cat is None:
        return f"No category matched '{category_name}'."
    # Allow assigning to ANY category — including goals / scheduled bills,
    # since those have to be funded each month. The is_spending=0 filter
    # only applies to the LLM auto-categorizer (it shouldn't *suggest* a
    # goal for a spontaneous charge), not to manual assignment.
    month = _current_month()
    state = envelope.assign_to_category(
        db_path, month, cat["id"], int(round(amount_dollars * 100)),
    )
    return (
        f"Assigned {_fmt_money(int(round(amount_dollars * 100)))} → {cat['name']} for {month}.\n"
        f"  New budgeted: {_fmt_money(state['budgeted_cents'])}\n"
        f"  New available: {_fmt_money(state['available_cents'])}"
    )


def apply_historical_budget_tool(
    db_path: str,
    *,
    months_lookback: int = 12,
    multiplier: float = 1.0,
    dry_run: bool = False,
) -> str:
    """Compute median outflow per category over last `months_lookback` months
    and set this month's budgeted_cents to that median × multiplier.

    Use this once at the start of a month to seed envelopes from history.
    Existing budgeted amounts are OVERWRITTEN (the tool wins). User can tweak
    individual categories with assign_to_category afterward.

    Returns a summary string with categories assigned + total.
    """
    import statistics
    from bot import envelope

    month = _current_month()
    with storage.connect(db_path) as con:
        spending_cats = con.execute(
            """SELECT c.id, c.name FROM category c
               WHERE c.hidden = 0 AND c.is_spending = 1"""
        ).fetchall()
        # Pull monthly outflows per category for the lookback window
        rows = con.execute(
            """SELECT category_id,
                      strftime('%Y-%m', posted_date) AS m,
                      SUM(amount_cents) AS total
               FROM ledger_txn
               WHERE category_id IS NOT NULL
                 AND posted_date >= date('now', ?)
               GROUP BY category_id, m""",
            (f"-{int(months_lookback)} months",),
        ).fetchall()

    by_cat: dict[str, list[int]] = {}
    for r in rows:
        # Only count months where there was activity. Outflows are negative.
        by_cat.setdefault(r["category_id"], []).append(int(r["total"]))

    plan: list[tuple[str, str, int]] = []  # (category_id, name, budgeted_cents)
    for cat in spending_cats:
        cid = cat["id"]
        months = by_cat.get(cid, [])
        if not months:
            continue
        # Median of monthly outflows (already negative). Take abs for budgeted.
        median_outflow = statistics.median(months)
        if median_outflow >= 0:
            continue  # category was net inflow or zero
        budgeted = int(round(abs(median_outflow) * multiplier))
        if budgeted < 100:  # less than a dollar — skip
            continue
        plan.append((cid, cat["name"], budgeted))

    if dry_run:
        lines = [f"Would budget for {month} (dry run):"]
        total = 0
        for _, name, b in plan:
            lines.append(f"  {_fmt_money(b):>10}  {name}")
            total += b
        lines.append(f"  {'-' * 10}")
        lines.append(f"  {_fmt_money(total):>10}  TOTAL")
        return "\n".join(lines)

    total = 0
    for cid, name, budgeted in plan:
        # Set the budget absolutely (not add). assign_to_category adds, so we
        # need to compute the delta from current.
        with storage.connect(db_path) as con:
            existing = con.execute(
                """SELECT budgeted_cents FROM month_category
                   WHERE month = ? AND category_id = ?""",
                (month, cid),
            ).fetchone()
        current = int(existing["budgeted_cents"]) if existing else 0
        delta = budgeted - current
        if delta != 0:
            envelope.assign_to_category(db_path, month, cid, delta)
        total += budgeted

    storage.audit(db_path, "applied_historical_budget",
                  {"month": month, "categories": len(plan), "total_cents": total,
                   "months_lookback": months_lookback, "multiplier": multiplier})
    lines = [f"Applied historical budget for {month} ({months_lookback}-month median × {multiplier}):"]
    for _, name, b in plan:
        lines.append(f"  {_fmt_money(b):>10}  {name}")
    lines.append(f"  {'-' * 10}")
    lines.append(f"  {_fmt_money(total):>10}  TOTAL")
    return "\n".join(lines)


def move_money_tool(
    db_path: str,
    *,
    from_category: str,
    to_category: str,
    amount_dollars: float,
) -> str:
    cents = int(round(amount_dollars * 100))
    if cents <= 0:
        return "Amount must be positive — say 'move 50 from dining to groceries', not -50."
    src = _resolve_category(db_path, from_category)
    dst = _resolve_category(db_path, to_category)
    if src is None:
        return f"No source category matched '{from_category}'."
    if dst is None:
        return f"No destination category matched '{to_category}'."
    if src["id"] == dst["id"]:
        return "Source and destination are the same category — nothing to do."
    month = _current_month()
    result = envelope.move_money(db_path, month, src["id"], dst["id"], cents)
    return (
        f"Moved {_fmt_money(cents)}: {src['name']} → {dst['name']}\n"
        f"  {src['name']}: now {_fmt_money(result['from']['available_cents'])} available\n"
        f"  {dst['name']}: now {_fmt_money(result['to']['available_cents'])} available"
    )


# ---------------------------------------------------------------------------
# In-flight categorization tools (Telegram pending item)
# ---------------------------------------------------------------------------

def categorize_pending_tool(
    db_path: str, *, chat_id: int, category_name: str,
) -> str:
    """Confirm the in-flight pending item with a named category."""
    # Look up what we last asked this chat about
    with storage.connect(db_path) as con:
        row = con.execute(
            """SELECT last_asked_kind, last_asked_id
               FROM bot_conversation WHERE chat_id = ?""",
            (chat_id,),
        ).fetchone()
    if not row or row["last_asked_id"] is None:
        return "Nothing's pending — try /pending."

    cat = _resolve_category(db_path, category_name)
    if cat is None:
        return f"No category matched '{category_name}'."

    table = "pending_order" if row["last_asked_kind"] == "order" else "pending_txn"
    with storage.connect(db_path) as con:
        if table == "pending_txn":
            con.execute(
                """UPDATE pending_txn
                   SET chosen_category = ?, status = 'categorized', chosen_at = ?
                   WHERE id = ?""",
                (cat["id"], datetime.now(), row["last_asked_id"]),
            )
        else:
            con.execute(
                """UPDATE pending_order
                   SET chosen_category = ?, status = 'categorized', chosen_at = ?,
                       updated_at = ?
                   WHERE id = ?""",
                (cat["id"], datetime.now(), datetime.now(), row["last_asked_id"]),
            )
        # Clear the in-flight pointer so push loop moves on
        con.execute(
            "UPDATE bot_conversation SET last_asked_id = NULL WHERE chat_id = ?",
            (chat_id,),
        )
    storage.audit(
        db_path, "categorized",
        {"kind": row["last_asked_kind"], "id": row["last_asked_id"],
         "category_id": cat["id"], "via": "agent"},
    )
    return f"Categorized as {cat['name']}. Moving to next."


def skip_pending_tool(db_path: str, *, chat_id: int) -> str:
    with storage.connect(db_path) as con:
        row = con.execute(
            """SELECT last_asked_kind, last_asked_id
               FROM bot_conversation WHERE chat_id = ?""",
            (chat_id,),
        ).fetchone()
    if not row or row["last_asked_id"] is None:
        return "Nothing's pending — try /pending."
    if row["last_asked_kind"] == "txn":
        with storage.connect(db_path) as con:
            con.execute(
                "UPDATE pending_txn SET status = 'skipped' WHERE id = ?",
                (row["last_asked_id"],),
            )
    with storage.connect(db_path) as con:
        con.execute(
            "UPDATE bot_conversation SET last_asked_id = NULL WHERE chat_id = ?",
            (chat_id,),
        )
    return "Skipped."


# ---------------------------------------------------------------------------
# Notification / report tools
# ---------------------------------------------------------------------------

def set_quiet_tool(db_path: str, *, chat_id: int, hours: int) -> str:
    if hours <= 0:
        # Resume
        with storage.connect(db_path) as con:
            con.execute(
                "UPDATE bot_conversation SET quiet_until = NULL WHERE chat_id = ?",
                (chat_id,),
            )
        return "Notifications resumed."
    until = datetime.now() + timedelta(hours=hours)
    with storage.connect(db_path) as con:
        con.execute(
            """INSERT INTO bot_conversation (chat_id, user_id, quiet_until)
               VALUES (?, '?', ?)
               ON CONFLICT(chat_id) DO UPDATE SET quiet_until = excluded.quiet_until""",
            (chat_id, until),
        )
    return f"Notifications paused for {hours}h."


# Sentinel string the telegram_bot detects to trigger an immediate
# _push_next_item after the agent reply. Anything starting with this
# prefix is consumed by the bot handler, not echoed to the user.
ADVANCE_QUEUE_SENTINEL = "__ADVANCE_QUEUE__"


def _title_case_category(raw: str) -> str:
    """Title-case a user-typed category name without mangling acronyms.

    Examples:
        "password manager"     -> "Password Manager"
        "1Password subscription" -> "1Password Subscription"
        "AT&T cell phone"      -> "AT&T Cell Phone"
        "  hoa fees "          -> "Hoa Fees" (caller likely overrides)
    """
    s = (raw or "").strip()
    if not s:
        return ""
    out = []
    for word in s.split():
        # Keep words already containing uppercase letters (acronyms,
        # brand names like 1Password / AT&T) verbatim. Otherwise capitalize.
        if any(ch.isupper() for ch in word[1:]):
            out.append(word)
        else:
            out.append(word[:1].upper() + word[1:].lower())
    return " ".join(out)


def next_pending_tool(db_path: str, *, chat_id: int) -> str:
    """Advance to the next pending item. Clears the in-flight pointer so
    the bot's push path can fetch the next queued question and DM it with
    the educated-guess keyboard. The telegram_bot detects the sentinel
    return value and triggers an immediate push instead of waiting for
    the 30s push-loop tick.

    If the queue is empty, returns a normal "all caught up" string.
    """
    with storage.connect(db_path) as con:
        # Are there any pending items at all?
        n_pending = con.execute(
            "SELECT COUNT(*) FROM pending_txn WHERE status = 'pending'"
        ).fetchone()[0]
        n_orders = con.execute(
            "SELECT COUNT(*) FROM pending_order WHERE status = 'pending'"
        ).fetchone()[0]
    if not n_pending and not n_orders:
        return "Queue is empty — nothing pending. ✨"
    # Clear the in-flight pointer so _push_next_item will pick the next row
    with storage.connect(db_path) as con:
        con.execute(
            "UPDATE bot_conversation SET last_asked_id = NULL "
            "WHERE chat_id = ?",
            (chat_id,),
        )
    # Sentinel tells telegram_bot to push immediately.
    return ADVANCE_QUEUE_SENTINEL


def create_category_tool(
    db_path: str, *, category_name: str, group_name: str | None = None,
    monthly_target_dollars: float | None = None,
    settings: Any = None,
) -> str:
    """Create a new YNAB category and mirror it locally.

    `group_name` is fuzzy-matched against the local category_group table
    (and the YNAB list as fallback). If ambiguous, the user is asked to
    disambiguate. Defaults to "Monthly Bills" if not provided — most
    user-driven creations are recurring service subscriptions.

    Marks the new category as `is_spending=1` only if it isn't placed
    under a known non-spending group (Credit Card Payments, Internal
    Master Category, etc.). User-created categories under Monthly Bills
    follow the "named goal / day-of-month" convention.
    """
    name = _title_case_category(category_name)
    if not name:
        return "Please give me a name for the new category."

    # Already exists? Don't double-create.
    existing = _resolve_category(db_path, name)
    if existing and existing["name"].strip().lower() == name.lower():
        return f"'{existing['name']}' already exists in {existing['group_name']}."

    # Resolve the group
    group_q = (group_name or "Monthly Bills").strip().lower()
    with storage.connect(db_path) as con:
        groups = con.execute(
            "SELECT id, name FROM category_group WHERE hidden = 0"
        ).fetchall()
    matches = [g for g in groups if group_q in g["name"].lower()]
    if not matches:
        return (
            f"Couldn't find a category group matching '{group_name or 'Monthly Bills'}'. "
            f"Try: " + ", ".join(g["name"] for g in groups[:6]) + "..."
        )
    if len(matches) > 1:
        # Prefer exact match
        exact = [g for g in matches if g["name"].lower() == group_q]
        if exact:
            matches = exact
        else:
            return (
                f"Multiple groups match '{group_name}': "
                + ", ".join(g["name"] for g in matches)
                + ". Be more specific?"
            )
    group = dict(matches[0])

    # Hit YNAB to actually create the category
    if settings is None or not settings.ynab_token:
        return (
            "Created locally as a placeholder, but I can't write to YNAB "
            "without the ynab token. Go create '{name}' under '{group}' in "
            "YNAB when you can."
        ).format(name=name, group=group["name"])
    from bot.ynab_client import YnabClient
    ynab_client = YnabClient(settings.ynab_token, settings.ynab.budget_id)
    goal_cents = (
        int(round(float(monthly_target_dollars) * 100))
        if monthly_target_dollars else None
    )
    try:
        created = ynab_client.create_category(
            name, group["id"], goal_target_cents=goal_cents,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("YNAB create_category failed: %s", e)
        return f"YNAB rejected the category creation: {e}"

    # Mirror to local table. Non-spending classification follows the same
    # rule as ynab_history_import — any group in the NON_SPENDING set, or
    # any name with a (1st)/(4th)/(12th)-style suffix.
    NON_SPENDING_GROUPS = {
        "Credit Card Payments", "Internal Master Category", "Monthly Bills",
        "Quarterly Bills", "Annual or Seasonal Costs", "Investments",
        "Savings", "Individual Vacations", "Business Fund",
        "Personal Spending",
    }
    is_spending = 0 if group["name"] in NON_SPENDING_GROUPS else 1
    import re
    if re.search(r"\(\d{1,2}(?:st|nd|rd|th)\)", name, re.IGNORECASE):
        is_spending = 0

    with storage.connect(db_path) as con:
        con.execute(
            """INSERT OR REPLACE INTO category
               (id, group_id, name, ynab_category_id, hidden, is_spending)
               VALUES (?, ?, ?, ?, 0, ?)""",
            (created["id"], created["group_id"], created["name"],
             created["id"], is_spending),
        )
    storage.audit(db_path, "category_created", {
        "id": created["id"], "name": created["name"],
        "group": group["name"], "is_spending": is_spending,
    })

    spending_note = "" if is_spending else " (won't be auto-suggested — bills/goals are user-funded)"
    goal_note = ""
    if goal_cents:
        goal_note = (
            f"\nMonthly need goal: {_fmt_money(goal_cents)}. "
            f"YNAB will prompt you to fund it each month."
        )
    return (
        f"Created '{created['name']}' under {group['name']}.{spending_note}"
        f"{goal_note}\n"
        f"You can now type that name on the next prompt to use it."
    )


def move_category_to_group_tool(
    db_path: str, *, category_name: str, group_name: str,
    settings: Any = None,
) -> str:
    """Move an existing YNAB category to a different category group.

    Use when the user says "move X to Annual or Seasonal", "put Password
    Manager under Annual Costs instead", "wrong group, should be Day to Day".
    Updates the local mirror and YNAB via PATCH.

    Also re-evaluates `is_spending` based on the new group: moving from
    "Monthly Bills" to "Day to Day Expenses" flips it to spending (so the
    LLM can suggest it for spontaneous transactions); the reverse flips
    it off.
    """
    cat = _resolve_category(db_path, category_name)
    if not cat:
        return (
            f"No category matching '{category_name}'. "
            f"Try `list categories` to see options."
        )

    # Resolve target group
    group_q = (group_name or "").strip().lower()
    if not group_q:
        return "Tell me which group to move it to."
    with storage.connect(db_path) as con:
        groups = con.execute(
            "SELECT id, name FROM category_group WHERE hidden = 0"
        ).fetchall()
    matches = [g for g in groups if group_q in g["name"].lower()]
    if not matches:
        return (
            f"Couldn't find a group matching '{group_name}'. "
            f"Available: " + ", ".join(g["name"] for g in groups[:8])
        )
    if len(matches) > 1:
        exact = [g for g in matches if g["name"].lower() == group_q]
        if exact:
            matches = exact
        else:
            return (
                f"Multiple groups match '{group_name}': "
                + ", ".join(g["name"] for g in matches)
                + ". Be more specific?"
            )
    new_group = dict(matches[0])

    if new_group["id"] == cat["group_id"]:
        return f"'{cat['name']}' is already in {new_group['name']}."

    # Push to YNAB
    if settings is None or not settings.ynab_token:
        return (
            f"Local-only mode (no YNAB token). Can't move '{cat['name']}' "
            f"to {new_group['name']}."
        )
    from bot.ynab_client import YnabClient
    ynab_client = YnabClient(settings.ynab_token, settings.ynab.budget_id)
    try:
        ynab_client.update_category(
            cat["id"], category_group_id=new_group["id"],
        )
    except Exception as e:  # noqa: BLE001
        log.exception("YNAB update_category failed: %s", e)
        return f"YNAB rejected the move: {e}"

    # Mirror locally + re-evaluate is_spending under the new group
    NON_SPENDING_GROUPS = {
        "Credit Card Payments", "Internal Master Category", "Monthly Bills",
        "Quarterly Bills", "Annual or Seasonal Costs", "Investments",
        "Savings", "Individual Vacations", "Business Fund",
        "Personal Spending",
    }
    is_spending = 0 if new_group["name"] in NON_SPENDING_GROUPS else 1
    import re
    if re.search(r"\(\d{1,2}(?:st|nd|rd|th)\)", cat["name"], re.IGNORECASE):
        is_spending = 0
    with storage.connect(db_path) as con:
        con.execute(
            "UPDATE category SET group_id = ?, is_spending = ? WHERE id = ?",
            (new_group["id"], is_spending, cat["id"]),
        )
    storage.audit(db_path, "category_moved", {
        "id": cat["id"], "name": cat["name"],
        "from_group": cat["group_name"], "to_group": new_group["name"],
        "is_spending": is_spending,
    })
    return (
        f"Moved '{cat['name']}' from {cat['group_name']} to {new_group['name']}."
    )


def unskip_pending_tool(
    db_path: str, *, since_hours: int | None = None,
    payee_filter: str | None = None,
) -> str:
    """Return previously-skipped pending_txn rows to the active queue.

    Use when the user says "revisit skipped", "let me look at what I
    skipped", "unskip everything", "go back through the skipped ones",
    "let me see them again".

    Optional filters:
      - ``since_hours``: only unskip rows where the SKIP happened within
        the last N hours (keyed off the audit_log). Defaults to all.
      - ``payee_filter``: restrict to payees matching this substring
        (case-insensitive). Useful for "show me the Etsy ones I skipped".

    Returns a one-line summary. The bot's push loop will start surfacing
    them on its next tick (~30s); user can also type "next" to grab one
    immediately.
    """
    where = ["status = 'skipped'"]
    bind: list = []
    if payee_filter:
        where.append("LOWER(payee) LIKE ?")
        bind.append(f"%{payee_filter.lower().strip()}%")

    ids_to_unskip: list[int] = []
    with storage.connect(db_path) as con:
        rows = con.execute(
            f"SELECT id FROM pending_txn WHERE {' AND '.join(where)}",
            bind,
        ).fetchall()
        candidate_ids = {r["id"] for r in rows}

        if since_hours is not None and since_hours > 0:
            audit_rows = con.execute(
                """SELECT details FROM audit_log
                   WHERE event = 'skipped'
                     AND ts >= datetime('now', ?)""",
                (f"-{int(since_hours)} hours",),
            ).fetchall()
            recently_skipped: set[int] = set()
            for ar in audit_rows:
                try:
                    d = json.loads(ar["details"] or "{}")
                    if d.get("kind") == "txn" and d.get("id") is not None:
                        recently_skipped.add(int(d["id"]))
                except (ValueError, TypeError):
                    continue
            ids_to_unskip = sorted(candidate_ids & recently_skipped)
        else:
            ids_to_unskip = sorted(candidate_ids)

    if not ids_to_unskip:
        return "Nothing to unskip — no skipped rows match your filter."

    with storage.connect(db_path) as con:
        con.executemany(
            "UPDATE pending_txn SET status = 'pending' WHERE id = ?",
            [(i,) for i in ids_to_unskip],
        )
    storage.audit(db_path, "unskip_batch", {
        "count": len(ids_to_unskip),
        "since_hours": since_hours,
        "payee_filter": payee_filter,
    })
    filter_note = ""
    if since_hours:
        filter_note += f" (skipped within last {since_hours}h)"
    if payee_filter:
        filter_note += f" (payee~{payee_filter!r})"
    return (
        f"Unskipped {len(ids_to_unskip)} transactions{filter_note}. "
        f"They're back in the queue. Type 'next' to grab one, "
        f"or wait for the push loop (~30s)."
    )


def rename_category_tool(
    db_path: str, *, category_name: str, new_name: str,
    settings: Any = None,
) -> str:
    """Rename an existing YNAB category. Mirrors locally.

    Use when the user says "rename X to Y", "change X's name to Z",
    "X should be called Y". Does NOT title-case the new name — the user
    chose the exact spelling.
    """
    cat = _resolve_category(db_path, category_name)
    if not cat:
        return f"No category matching '{category_name}'."
    new = (new_name or "").strip()
    if not new:
        return "Tell me the new name."
    if new == cat["name"]:
        return f"'{cat['name']}' is already named that."

    if settings is None or not settings.ynab_token:
        return f"Local-only mode (no YNAB token). Can't rename '{cat['name']}'."
    from bot.ynab_client import YnabClient
    ynab_client = YnabClient(settings.ynab_token, settings.ynab.budget_id)
    try:
        ynab_client.update_category(cat["id"], name=new)
    except Exception as e:  # noqa: BLE001
        log.exception("YNAB rename failed: %s", e)
        return f"YNAB rejected the rename: {e}"

    with storage.connect(db_path) as con:
        con.execute("UPDATE category SET name = ? WHERE id = ?",
                    (new, cat["id"]))
    storage.audit(db_path, "category_renamed", {
        "id": cat["id"], "old_name": cat["name"], "new_name": new,
    })
    return f"Renamed '{cat['name']}' → '{new}'."


def show_summary_tool(db_path: str, *, period: str = "today") -> str:
    """Placeholder until Phase 5 reporters/daily.py + reporters/weekly.py land.

    For now, returns a snapshot of current month's tight envelopes + last-7-day
    transaction count.
    """
    if period not in {"today", "yesterday", "this_week"}:
        period = "today"
    month = _current_month()
    with storage.connect(db_path) as con:
        # Tight categories: available < 25% of budgeted (when there's a budget)
        tight = con.execute(
            """SELECT c.name, mc.available_cents, mc.budgeted_cents
               FROM month_category mc
               JOIN category c ON c.id = mc.category_id
               WHERE mc.month = ?
                 AND mc.budgeted_cents > 0
                 AND mc.available_cents * 4 < mc.budgeted_cents
               ORDER BY mc.available_cents ASC LIMIT 5""",
            (month,),
        ).fetchall()
        overspent = con.execute(
            """SELECT c.name, mc.available_cents
               FROM month_category mc
               JOIN category c ON c.id = mc.category_id
               WHERE mc.month = ? AND mc.available_cents < 0
               ORDER BY mc.available_cents ASC LIMIT 5""",
            (month,),
        ).fetchall()
        recent = con.execute(
            """SELECT COUNT(*) AS n, COALESCE(SUM(amount_cents), 0) AS total
               FROM ledger_txn
               WHERE posted_date >= date('now', '-7 days')""",
        ).fetchone()

    lines = [f"📅 Snapshot for {month}"]
    lines.append(
        f"Last 7 days: {recent['n']} transactions, "
        f"net {_fmt_money(int(recent['total']))}"
    )
    if overspent:
        lines.append("\n🔴 Overspent:")
        for o in overspent:
            lines.append(f"  {o['name']}: {_fmt_money(int(o['available_cents']))}")
    if tight:
        lines.append("\n🟡 Running tight:")
        for t in tight:
            lines.append(
                f"  {t['name']}: {_fmt_money(int(t['available_cents']))} of "
                f"{_fmt_money(int(t['budgeted_cents']))} budgeted"
            )
    if not overspent and not tight:
        lines.append("\n🟢 Nothing tight or overspent this month.")
    return "\n".join(lines)
