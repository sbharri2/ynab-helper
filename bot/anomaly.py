"""Anomaly detection helpers for Phase 5 weekly summaries.

Two flavors:
  - `z_score_for_payee`: how unusual is this week's spending at a given payee
    vs the same payee's historical median? Uses MAD (median absolute deviation)
    for a robust non-Gaussian measure.
  - `categories_burning_fast`: which categories are on track to exceed their
    monthly budget before month-end, based on cumulative spend vs days elapsed.

Both are pure read functions over `ledger_txn` + `month_category`. No LLM,
no email, no Telegram.
"""
from __future__ import annotations

import calendar
import logging
import statistics
from datetime import date, timedelta

from bot import storage

log = logging.getLogger(__name__)


def z_score_for_payee(
    db_path: str,
    payee: str,
    *,
    window_weeks: int = 12,
) -> dict:
    """Compare this week's spending at `payee` against a rolling median.

    Returns:
      {
        "payee": str,
        "this_week_cents": int,    # negative = outflow
        "median_cents": int,
        "mad_cents": int,
        "z": float | None,         # rough robust z = (x - median) / (1.4826 * MAD)
        "weeks_observed": int,
      }

    `z` is None when there's not enough history. Treat |z| > 2 as noteworthy.
    """
    cutoff_start = date.today() - timedelta(days=window_weeks * 7)
    week_start = date.today() - timedelta(days=date.today().weekday())

    with storage.connect(db_path) as con:
        # Pull all transactions for this payee in the window. We bucket by
        # ISO week and sum.
        rows = con.execute(
            """SELECT posted_date, amount_cents
               FROM ledger_txn
               WHERE LOWER(payee) LIKE ?
                 AND posted_date >= ?""",
            (f"%{payee.lower()}%", cutoff_start),
        ).fetchall()

    weekly: dict[date, int] = {}
    for r in rows:
        d = r["posted_date"]
        if not isinstance(d, date):
            d = date.fromisoformat(str(d))
        week_key = d - timedelta(days=d.weekday())  # Monday of that week
        weekly[week_key] = weekly.get(week_key, 0) + int(r["amount_cents"])

    if not weekly:
        return {
            "payee": payee,
            "this_week_cents": 0,
            "median_cents": 0,
            "mad_cents": 0,
            "z": None,
            "weeks_observed": 0,
        }

    this_week = weekly.get(week_start, 0)
    historical = [v for k, v in weekly.items() if k < week_start]
    if len(historical) < 4:
        # Not enough history for a meaningful baseline
        return {
            "payee": payee,
            "this_week_cents": this_week,
            "median_cents": 0,
            "mad_cents": 0,
            "z": None,
            "weeks_observed": len(historical),
        }

    median = statistics.median(historical)
    mad = statistics.median([abs(v - median) for v in historical]) or 1
    z = (this_week - median) / (1.4826 * mad)
    return {
        "payee": payee,
        "this_week_cents": this_week,
        "median_cents": int(median),
        "mad_cents": int(mad),
        "z": z,
        "weeks_observed": len(historical),
    }


def categories_burning_fast(
    db_path: str, month: str | None = None,
) -> list[dict]:
    """Categories on track to overspend by month-end.

    Compares `pace = activity / budgeted` against `time_elapsed = day / days_in_month`.
    If pace > time_elapsed * 1.10, the category will likely overspend.

    Returns rows sorted by overspend ratio descending.
    """
    today = date.today()
    if month is None:
        month = today.strftime("%Y-%m")
    year, mo = map(int, month.split("-"))
    _, days_in_month = calendar.monthrange(year, mo)
    if (today.year, today.month) == (year, mo):
        day_of_month = today.day
    else:
        # Past month → fully elapsed
        day_of_month = days_in_month
    time_elapsed = day_of_month / days_in_month

    with storage.connect(db_path) as con:
        rows = con.execute(
            """SELECT c.id, c.name, g.name AS group_name,
                      mc.budgeted_cents, mc.activity_cents
               FROM month_category mc
               JOIN category c ON c.id = mc.category_id
               JOIN category_group g ON g.id = c.group_id
               WHERE mc.month = ?
                 AND mc.budgeted_cents > 0
                 AND c.is_spending = 1""",
            (month,),
        ).fetchall()

    results = []
    for r in rows:
        budgeted = int(r["budgeted_cents"])
        spent = -int(r["activity_cents"]) if int(r["activity_cents"]) < 0 else 0
        if budgeted == 0:
            continue
        pace = spent / budgeted
        if pace > time_elapsed * 1.10 and spent > 0:
            projected_total = spent / time_elapsed if time_elapsed > 0 else spent
            results.append({
                "category_id": r["id"],
                "category_name": r["name"],
                "group_name": r["group_name"],
                "budgeted_cents": budgeted,
                "spent_cents": spent,
                "pace_ratio": round(pace / max(time_elapsed, 0.01), 2),
                "projected_total_cents": int(projected_total),
            })

    results.sort(key=lambda x: -x["pace_ratio"])
    return results
