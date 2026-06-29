"""Cover overspends month-by-month, category-by-category.

After ``scripts/apply_budgets_history`` set baseline budgets for past
months, several categories are still negative-available because actual
spending outran the proposed median. This script bumps budgeted by
exactly abs(balance) for each overspent row so YNAB's ``available`` zeros
out.

YNAB's ``balance`` field on a category-month already reflects:
  prior_carryover + this_month_budgeted + this_month_activity

So bumping ``budgeted`` by abs(negative balance) gives balance=0.

Default DRY RUN. Pass --apply to write. Per-month preview is grouped:

    2026-02  total overspend: $-453.10
       Groceries           bal=-$87.50  bump=$87.50  → new_budgeted=$1,149.50
       Medical             bal=-$45.60  ...
       ...

Usage:
    python -m scripts.cover_overspends                  # dry-run Feb-Jun
    python -m scripts.cover_overspends --apply           # write
    python -m scripts.cover_overspends --months 2026-05   # only one month
    python -m scripts.cover_overspends --buffer-cents 0   # set to 0 buffer
"""
from __future__ import annotations

import argparse
import logging
from datetime import date
from pathlib import Path

import ynab

from bot import storage
from bot.config import load_settings

log = logging.getLogger("cover_overspends")

DEFAULT_TARGET_MONTHS = [
    "2026-02", "2026-03", "2026-04", "2026-05", "2026-06",
]


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    p.add_argument("--months", default=",".join(DEFAULT_TARGET_MONTHS),
                   help="Comma-separated YYYY-MM months to cover.")
    p.add_argument("--buffer-cents", type=int, default=0,
                   help="Extra cents to add on top of the overspend (default 0).")
    p.add_argument("--exclude", default="",
                   help="Comma-separated category-name substrings to skip.")
    return p.parse_args()


def _cents_from_milliunits(m: int) -> int:
    return m // 10


def _milliunits_from_cents(c: int) -> int:
    return c * 10


def fetch_overspent(client, plan_id: str, month_str: str,
                    exclude_terms: list[str]) -> list[dict]:
    """Returns the category rows for a month whose balance < 0.

    Each entry:
      {category_id, name, group_name, budgeted_cents, activity_cents,
       balance_cents}
    """
    api = ynab.MonthsApi(client)
    resp = api.get_plan_month(
        plan_id=plan_id,
        month=date.fromisoformat(month_str + "-01"),
    )
    out: list[dict] = []
    for c in resp.data.month.categories:
        if c.hidden or c.deleted:
            continue
        balance = _cents_from_milliunits(c.balance)
        if balance >= 0:
            continue
        if any(t in c.name.lower() for t in exclude_terms):
            continue
        out.append({
            "category_id": str(c.id),
            "name": c.name,
            "budgeted_cents": _cents_from_milliunits(c.budgeted),
            "activity_cents": _cents_from_milliunits(c.activity),
            "balance_cents": balance,
        })
    return out


def cover_row(client, plan_id: str, month_str: str,
              row: dict, buffer_cents: int) -> int:
    """PATCH the category to bump budgeted by abs(balance) + buffer.

    Returns the new budgeted_cents.
    """
    bump = abs(row["balance_cents"]) + buffer_cents
    new_budgeted = row["budgeted_cents"] + bump
    api = ynab.CategoriesApi(client)
    api.update_month_category(
        plan_id=plan_id,
        month=date.fromisoformat(month_str + "-01"),
        category_id=row["category_id"],
        data=ynab.PatchMonthCategoryWrapper(
            category=ynab.SaveMonthCategory(
                budgeted=_milliunits_from_cents(new_budgeted),
            ),
        ),
    )
    return new_budgeted


def mirror_local(db_path: str, month_str: str, category_id: str,
                 budgeted_cents: int, activity_cents: int) -> None:
    available_cents = budgeted_cents + activity_cents
    with storage.connect(db_path) as con:
        con.execute(
            """INSERT INTO month_category
               (month, category_id, budgeted_cents, activity_cents, available_cents)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(month, category_id) DO UPDATE SET
                 budgeted_cents = excluded.budgeted_cents,
                 activity_cents = excluded.activity_cents,
                 available_cents = excluded.available_cents""",
            (month_str, category_id, budgeted_cents, activity_cents,
             available_cents),
        )


def main():
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = load_settings()
    target_months = args.months.split(",")
    exclude_terms = [
        s.strip().lower() for s in args.exclude.split(",") if s.strip()
    ]

    cfg = ynab.Configuration(access_token=settings.ynab_token)
    grand_total_bump = 0
    grand_total_rows = 0
    print(f"{'Month':10s} | {'category':35s} | {'balance':>10s} | {'bump':>10s} | "
          f"{'new budget':>12s}")
    print("-" * 90)
    with ynab.ApiClient(cfg) as client:
        for m in target_months:
            rows = fetch_overspent(client, settings.ynab.budget_id, m,
                                    exclude_terms)
            if not rows:
                print(f"{m}: nothing overspent ✓")
                continue
            rows.sort(key=lambda r: r["balance_cents"])  # most negative first
            month_total = sum(abs(r["balance_cents"]) + args.buffer_cents
                              for r in rows)
            for r in rows:
                bump = abs(r["balance_cents"]) + args.buffer_cents
                new_budgeted = r["budgeted_cents"] + bump
                print(f"{m:10s} | {r['name'][:33]:35s} | "
                      f"${r['balance_cents']/100:>+9.2f} | "
                      f"${bump/100:>9.2f} | ${new_budgeted/100:>11.2f}")
                if args.apply:
                    try:
                        cover_row(client, settings.ynab.budget_id, m, r,
                                  args.buffer_cents)
                        mirror_local(settings.paths.database, m,
                                     r["category_id"], new_budgeted,
                                     r["activity_cents"])
                        grand_total_rows += 1
                    except Exception as e:  # noqa: BLE001
                        log.warning("apply failed for %s/%s: %s",
                                    m, r["name"], e)
            print(f"{m:10s} subtotal: ${month_total/100:,.2f} across "
                  f"{len(rows)} categor{'ies' if len(rows) != 1 else 'y'}")
            print()
            grand_total_bump += month_total

    print("=" * 90)
    print(f"Grand total bump needed: ${grand_total_bump/100:,.2f}")
    if args.apply:
        print(f"Applied: {grand_total_rows} rows")
        storage.audit(settings.paths.database, "cover_overspends", {
            "months": target_months,
            "rows_applied": grand_total_rows,
            "total_cents": grand_total_bump,
            "buffer_cents": args.buffer_cents,
        })
    else:
        print("DRY RUN — pass --apply to actually patch YNAB + local DB.")


if __name__ == "__main__":
    main()
