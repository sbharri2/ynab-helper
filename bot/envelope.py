"""YNAB-style envelope budgeting math over `month_category`.

A category's `available_cents` for a given month is:

    available[m] = available[m-1] + budgeted[m] + activity[m]

where:
  - `available[m-1]` is the carryover from the prior month (or 0 if first month)
  - `budgeted[m]` is how much the user has assigned this month
  - `activity[m]` is the sum of `ledger_txn.amount_cents` for this category in
    this month (negative for spending, positive for refunds)

Amounts are signed: outflows are negative. The math works because YNAB stores
all amounts the same way the ledger does.

All functions are pure-ish — they read from and write to the database via
`bot.storage.connect`, but never talk to YNAB, Telegram, or Ollama. Safe to
call from tests, scripts, or the agent layer.
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Iterable

from bot import storage

log = logging.getLogger(__name__)


def _month_str(d: date) -> str:
    """ISO-style 'YYYY-MM' month key matching the schema's `month` column."""
    return d.strftime("%Y-%m")


def _prev_month(m: str) -> str:
    y, mo = map(int, m.split("-"))
    if mo == 1:
        return f"{y - 1:04d}-12"
    return f"{y:04d}-{mo - 1:02d}"


def _next_month(m: str) -> str:
    y, mo = map(int, m.split("-"))
    if mo == 12:
        return f"{y + 1:04d}-01"
    return f"{y:04d}-{mo + 1:02d}"


def _all_categories(con) -> list[str]:
    return [r["id"] for r in con.execute("SELECT id FROM category")]


def _activity_for_month(con, month: str, category_id: str) -> int:
    # Only on-budget accounts contribute to envelope activity. Money moving
    # through a TRACKING account (e.g. the off-budget Marcus emergency fund —
    # deposits, T-bill buys, crypto DCA) is not budget spending, exactly as
    # YNAB treats it. Without this, an off-budget account's categorized
    # transactions would pollute the envelopes. is_split = 0 keeps split
    # parents out (their children carry the category + amount).
    row = con.execute(
        """SELECT COALESCE(SUM(lt.amount_cents), 0) AS sum_cents
           FROM ledger_txn lt
           JOIN account a ON a.id = lt.account_id
           WHERE lt.category_id = ?
             AND strftime('%Y-%m', lt.posted_date) = ?
             AND a.on_budget = 1
             AND lt.is_split = 0""",
        (category_id, month),
    ).fetchone()
    return int(row["sum_cents"] or 0)


def _prior_available(con, month: str, category_id: str) -> int:
    prev = _prev_month(month)
    row = con.execute(
        """SELECT available_cents
           FROM month_category
           WHERE month = ? AND category_id = ?""",
        (prev, category_id),
    ).fetchone()
    return int(row["available_cents"]) if row else 0


def _upsert_month_category(
    con,
    *,
    month: str,
    category_id: str,
    budgeted_cents: int,
    activity_cents: int,
    available_cents: int,
) -> None:
    con.execute(
        """INSERT INTO month_category
             (month, category_id, budgeted_cents, activity_cents, available_cents)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(month, category_id) DO UPDATE SET
             budgeted_cents = excluded.budgeted_cents,
             activity_cents = excluded.activity_cents,
             available_cents = excluded.available_cents""",
        (month, category_id, budgeted_cents, activity_cents, available_cents),
    )


def recompute_month(
    db_path: Path | str,
    month: str,
    *,
    category_ids: Iterable[str] | None = None,
) -> dict[str, int]:
    """Recompute activity + available for every (or given) category in `month`.

    Reads ledger_txn for the month and updates month_category accordingly.
    Preserves the existing `budgeted_cents` value (use `assign_to_category` to
    change that). Returns {category_id: available_cents} for what was touched.

    Idempotent — safe to run any time. Doesn't roll forward; for that, also
    call `recompute_month(next_month)` after this.
    """
    results: dict[str, int] = {}
    with storage.connect(db_path) as con:
        cats = list(category_ids) if category_ids else _all_categories(con)
        for category_id in cats:
            activity = _activity_for_month(con, month, category_id)
            existing = con.execute(
                """SELECT budgeted_cents FROM month_category
                   WHERE month = ? AND category_id = ?""",
                (month, category_id),
            ).fetchone()
            budgeted = int(existing["budgeted_cents"]) if existing else 0
            prior_avail = _prior_available(con, month, category_id)
            available = prior_avail + budgeted + activity
            _upsert_month_category(
                con,
                month=month,
                category_id=category_id,
                budgeted_cents=budgeted,
                activity_cents=activity,
                available_cents=available,
            )
            results[category_id] = available
    return results


def available_for_category(
    db_path: Path | str,
    *,
    category_id: str,
    month: str | None = None,
) -> int:
    """Recompute and return available_cents for one category this month.

    Used by the bot's "Categorized as X — $Y left in pot" confirmation so
    Steven sees the envelope balance immediately after he categorizes.
    Cheaper than full recompute_month because it only touches one row.
    """
    if month is None:
        month = date.today().strftime("%Y-%m")
    results = recompute_month(db_path, month, category_ids=[category_id])
    return results.get(category_id, 0)


def roll_forward(
    db_path: Path | str,
    from_month: str,
    to_month: str | None = None,
) -> int:
    """Initialize a future month's `available` from a prior month's leftovers.

    For every category that has a row in `from_month`, ensure a row exists in
    `to_month` whose available_cents starts as `from_month.available_cents`
    plus any `to_month` activity already accumulated. `budgeted_cents` defaults
    to 0 — the user assigns later.

    Returns the number of (month, category) rows touched.

    If `to_month` is None, defaults to the month after `from_month`.
    """
    if to_month is None:
        to_month = _next_month(from_month)

    touched = 0
    with storage.connect(db_path) as con:
        rows = con.execute(
            """SELECT category_id, available_cents
               FROM month_category WHERE month = ?""",
            (from_month,),
        ).fetchall()
        for r in rows:
            category_id = r["category_id"]
            prior_avail = int(r["available_cents"])
            activity = _activity_for_month(con, to_month, category_id)
            existing = con.execute(
                """SELECT budgeted_cents FROM month_category
                   WHERE month = ? AND category_id = ?""",
                (to_month, category_id),
            ).fetchone()
            budgeted = int(existing["budgeted_cents"]) if existing else 0
            available = prior_avail + budgeted + activity
            _upsert_month_category(
                con,
                month=to_month,
                category_id=category_id,
                budgeted_cents=budgeted,
                activity_cents=activity,
                available_cents=available,
            )
            touched += 1
    return touched


def assign_to_category(
    db_path: Path | str,
    month: str,
    category_id: str,
    cents: int,
) -> dict:
    """Add `cents` to a category's `budgeted_cents` for the month.

    Positive `cents` adds, negative subtracts. Recomputes available_cents.
    Returns {budgeted_cents, activity_cents, available_cents} after the change.

    Note: this does NOT touch a "Ready to Assign" pool — that's the user's
    mental model in YNAB but in this ledger, "Ready to Assign" is implicit
    (total inflows - sum of all budgeted). If you want true RTA tracking,
    surface it in reports, not in this math.
    """
    with storage.connect(db_path) as con:
        existing = con.execute(
            """SELECT budgeted_cents FROM month_category
               WHERE month = ? AND category_id = ?""",
            (month, category_id),
        ).fetchone()
        old_budgeted = int(existing["budgeted_cents"]) if existing else 0
        new_budgeted = old_budgeted + cents

        activity = _activity_for_month(con, month, category_id)
        prior_avail = _prior_available(con, month, category_id)
        available = prior_avail + new_budgeted + activity

        _upsert_month_category(
            con,
            month=month,
            category_id=category_id,
            budgeted_cents=new_budgeted,
            activity_cents=activity,
            available_cents=available,
        )
        return {
            "budgeted_cents": new_budgeted,
            "activity_cents": activity,
            "available_cents": available,
        }


def move_money(
    db_path: Path | str,
    month: str,
    from_category_id: str,
    to_category_id: str,
    cents: int,
) -> dict:
    """Move `cents` of budget from one envelope to another within `month`.

    Equivalent to two `assign_to_category` calls but transactional. Returns
    {from: {...}, to: {...}} with the new state of each envelope.
    """
    if cents <= 0:
        raise ValueError("move_money requires positive cents")
    with storage.connect(db_path) as con:
        # `from` loses budgeted
        from_existing = con.execute(
            """SELECT budgeted_cents FROM month_category
               WHERE month = ? AND category_id = ?""",
            (month, from_category_id),
        ).fetchone()
        from_old = int(from_existing["budgeted_cents"]) if from_existing else 0
        from_new = from_old - cents
        from_activity = _activity_for_month(con, month, from_category_id)
        from_prior = _prior_available(con, month, from_category_id)
        from_available = from_prior + from_new + from_activity
        _upsert_month_category(
            con,
            month=month,
            category_id=from_category_id,
            budgeted_cents=from_new,
            activity_cents=from_activity,
            available_cents=from_available,
        )

        # `to` gains budgeted
        to_existing = con.execute(
            """SELECT budgeted_cents FROM month_category
               WHERE month = ? AND category_id = ?""",
            (month, to_category_id),
        ).fetchone()
        to_old = int(to_existing["budgeted_cents"]) if to_existing else 0
        to_new = to_old + cents
        to_activity = _activity_for_month(con, month, to_category_id)
        to_prior = _prior_available(con, month, to_category_id)
        to_available = to_prior + to_new + to_activity
        _upsert_month_category(
            con,
            month=month,
            category_id=to_category_id,
            budgeted_cents=to_new,
            activity_cents=to_activity,
            available_cents=to_available,
        )

        return {
            "from": {
                "category_id": from_category_id,
                "budgeted_cents": from_new,
                "activity_cents": from_activity,
                "available_cents": from_available,
            },
            "to": {
                "category_id": to_category_id,
                "budgeted_cents": to_new,
                "activity_cents": to_activity,
                "available_cents": to_available,
            },
        }


# Groups that never participate in the month reconcile — sinking funds
# and mechanics, not spending envelopes (Steven, 2026-07-11: "savings
# doesn't make sense with this"). Reimbursables float negative by design
# until paid back; CC payment cats are YNAB plumbing.
RECONCILE_EXCLUDED_GROUPS = frozenset({
    "Internal Master Category",
    "Investments / Savings Transfers",
    "Investments",
    "Savings",
    "Annual or Seasonal Costs",
    "Credit Card Payments",
    "Reimbursables",
})

# Sinking-fund categories that live INSIDE spending groups, so the
# group-level exclusion can't see them. Their carryover is intentional
# accumulation — top-up must not drain their assignments. (A per-category
# "protected" flag in the DB is the eventual home for this list.)
PROTECTED_CATEGORY_NAMES = frozenset({
    "vacation",
    "steven personal savings",
    "allison personal savings",
})


def top_up_month(
    db_path: Path | str,
    month: str,
    *,
    dry_run: bool = True,
) -> dict:
    """Stop double-funding envelopes that already carried money in
    (Steven, 2026-07-19: "if 430 rolls over... it should stand to logic
    that 430 less should be contributed towards the budget in july").

    For every SPENDING category with budgeted > 0 and a positive
    carryover from the prior month: reduce budgeted to
    max(0, budgeted - carried). The envelope still holds its usual
    target for the month (carried + new budgeted == old target); the
    freed slack returns to Ready to Assign implicitly. Sinking-fund
    groups (RECONCILE_EXCLUDED_GROUPS) are meant to accumulate and are
    never touched.

    Returns the reduction plan; with dry_run=False also applies it via
    assign_to_category and audits.
    """
    with storage.connect(db_path) as con:
        rows = con.execute(
            """SELECT mc.category_id, c.name, g.name AS group_name,
                      mc.budgeted_cents, mc.activity_cents,
                      mc.available_cents
               FROM month_category mc
               JOIN category c ON c.id = mc.category_id
               JOIN category_group g ON g.id = c.group_id
               WHERE mc.month = ? AND c.hidden = 0 AND g.hidden = 0""",
            (month,),
        ).fetchall()

    reductions: list[dict] = []
    for r in rows:
        if r["group_name"] in RECONCILE_EXCLUDED_GROUPS:
            continue
        if r["name"].strip().lower() in PROTECTED_CATEGORY_NAMES:
            continue
        budgeted = int(r["budgeted_cents"])
        # Envelope identity: available = carried + budgeted + activity,
        # so the carryover needs no prior-month lookup.
        carried = (int(r["available_cents"]) - budgeted
                   - int(r["activity_cents"]))
        if budgeted <= 0 or carried <= 0:
            continue
        new_budgeted = max(0, budgeted - carried)
        reductions.append({
            "category_id": r["category_id"],
            "name": r["name"],
            "group_name": r["group_name"],
            "carried_cents": carried,
            "budgeted_cents": budgeted,
            "new_budgeted_cents": new_budgeted,
            "freed_cents": budgeted - new_budgeted,
        })

    reductions.sort(key=lambda x: -x["freed_cents"])
    freed = sum(x["freed_cents"] for x in reductions)

    if not dry_run:
        for x in reductions:
            assign_to_category(db_path, month, x["category_id"],
                               -x["freed_cents"])
        storage.audit(db_path, "top_up_month", {
            "month": month, "reductions": len(reductions),
            "freed_cents": freed,
        })

    return {
        "month": month,
        "dry_run": dry_run,
        "reductions": reductions,
        "freed_cents": freed,
    }


def reconcile_overspending(
    db_path: Path | str,
    month: str,
    *,
    dry_run: bool = True,
) -> dict:
    """Settle a month's envelope score in one shot (Steven, 2026-07-11:
    a "reconcile" button that nets surplus envelopes against negative
    ones).

    For every SPENDING category with available < 0 in `month`: cover it
    to $0, funded first by same-group categories with surplus (largest
    first), then from Ready to Assign for any remainder. Sinking-fund
    groups (RECONCILE_EXCLUDED_GROUPS) are never touched on either side.

    Returns the move plan; with dry_run=False also applies it via
    move_money / assign_to_category and audits.
    """
    with storage.connect(db_path) as con:
        rows = con.execute(
            """SELECT mc.category_id, c.name, g.name AS group_name,
                      mc.available_cents
               FROM month_category mc
               JOIN category c ON c.id = mc.category_id
               JOIN category_group g ON g.id = c.group_id
               WHERE mc.month = ? AND c.hidden = 0 AND g.hidden = 0""",
            (month,),
        ).fetchall()

    by_group: dict[str, list[dict]] = {}
    for r in rows:
        if r["group_name"] in RECONCILE_EXCLUDED_GROUPS:
            continue
        by_group.setdefault(r["group_name"], []).append(dict(r))

    moves: list[dict] = []
    for group_name, cats in sorted(by_group.items()):
        targets = sorted(
            (c for c in cats if c["available_cents"] < 0),
            key=lambda c: c["available_cents"],
        )
        sources = sorted(
            (dict(c) for c in cats if c["available_cents"] > 0),
            key=lambda c: -c["available_cents"],
        )
        for t in targets:
            need = -t["available_cents"]
            for s in sources:
                if need <= 0:
                    break
                take = min(s["available_cents"], need)
                if take <= 0:
                    continue
                s["available_cents"] -= take
                need -= take
                moves.append({
                    "from_category_id": s["category_id"],
                    "from_name": s["name"],
                    "to_category_id": t["category_id"],
                    "to_name": t["name"],
                    "group_name": group_name,
                    "cents": take,
                })
            if need > 0:
                moves.append({
                    "from_category_id": None,
                    "from_name": "Ready to Assign",
                    "to_category_id": t["category_id"],
                    "to_name": t["name"],
                    "group_name": group_name,
                    "cents": need,
                })

    rta_cents = sum(m["cents"] for m in moves if m["from_category_id"] is None)
    covered = sum(m["cents"] for m in moves)

    if not dry_run:
        for m in moves:
            if m["from_category_id"] is None:
                assign_to_category(db_path, month, m["to_category_id"],
                                   m["cents"])
            else:
                move_money(db_path, month,
                           from_category_id=m["from_category_id"],
                           to_category_id=m["to_category_id"],
                           cents=m["cents"])
        storage.audit(db_path, "reconcile_month", {
            "month": month, "moves": len(moves),
            "covered_cents": covered, "from_rta_cents": rta_cents,
        })

    return {
        "month": month,
        "dry_run": dry_run,
        "moves": moves,
        "covered_cents": covered,
        "from_rta_cents": rta_cents,
    }
