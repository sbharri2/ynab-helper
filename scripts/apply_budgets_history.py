"""Retroactively assign per-category budgets to past months from historical pattern.

Background: Steven manually budgeted through Feb 2026, then stopped.
March through June 2026 have zero budgeted across every category, which
makes the daily summary's "overspent" section look chaotic. This script
backfills those months using the median of the prior 6 months
(Aug 2025 — Jan 2026) as the "normal" per-category budget.

Pushes to YNAB via the `update_month_category` SDK endpoint AND
mirrors into the local `month_category` table so the bot's envelope
math reflects the new budgets immediately.

Default behavior is DRY RUN — prints the per-month per-category proposal
and asks no questions. Pass --apply to actually write.

Usage:
    python -m scripts.apply_budgets_history                  # dry-run
    python -m scripts.apply_budgets_history --apply           # write
    python -m scripts.apply_budgets_history --months 2026-03,2026-04
"""
from __future__ import annotations

import argparse
import logging
import statistics
from datetime import date
from pathlib import Path

import ynab

from bot import storage
from bot.config import load_settings

log = logging.getLogger("apply_budgets_history")

# Default target months — the ones currently at $0
DEFAULT_TARGET_MONTHS = ["2026-03", "2026-04", "2026-05", "2026-06"]
# Prior 6 months — the pattern we'll learn from
DEFAULT_LEARN_MONTHS = [
    "2025-08", "2025-09", "2025-10", "2025-11", "2025-12", "2026-01",
]


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true",
                   help="Actually write to YNAB + local DB. Default is dry-run.")
    p.add_argument("--months", default=",".join(DEFAULT_TARGET_MONTHS),
                   help="Comma-separated target months (YYYY-MM).")
    p.add_argument("--learn-months", default=",".join(DEFAULT_LEARN_MONTHS),
                   help="Comma-separated months to learn pattern from.")
    p.add_argument("--multiplier", type=float, default=1.0,
                   help="Scale proposed budgets by this factor (default 1.0).")
    p.add_argument("--exclude", default="",
                   help="Comma-separated category-name substrings to skip. "
                        "Case-insensitive match against the YNAB category name.")
    return p.parse_args()


def _milliunits_from_cents(c: int) -> int:
    return c * 10


def _cents_from_milliunits(m: int) -> int:
    return m // 10


def fetch_month_budgets(client, plan_id: str, month_str: str) -> dict[str, dict]:
    """Return {category_id: {name, group, budgeted_cents}} for a single month."""
    api = ynab.MonthsApi(client)
    resp = api.get_plan_month(
        plan_id=plan_id,
        month=date.fromisoformat(month_str + "-01"),
    )
    out: dict[str, dict] = {}
    for c in resp.data.month.categories:
        if c.hidden or c.deleted:
            continue
        out[str(c.id)] = {
            "name": c.name,
            "budgeted_cents": _cents_from_milliunits(c.budgeted),
        }
    return out


def compute_proposal(learn_data: dict[str, dict[str, dict]],
                     target_months: list[str],
                     multiplier: float) -> dict[str, dict[str, int]]:
    """{target_month: {category_id: proposed_cents}}.

    For each category, proposed = median of the non-zero budgets across
    the learn months. Categories that were never budgeted in any learn
    month default to 0. We don't propose categories that were always 0.
    """
    # Collect all category_ids seen in any learn month
    all_cat_ids = set()
    for m, cats in learn_data.items():
        all_cat_ids.update(cats.keys())

    proposals: dict[str, dict[str, int]] = {}
    for target in target_months:
        proposals[target] = {}
        for cid in all_cat_ids:
            vals = []
            for lm in learn_data:
                b = learn_data[lm].get(cid, {}).get("budgeted_cents", 0)
                if b > 0:
                    vals.append(b)
            if not vals:
                continue
            median = int(statistics.median(vals))
            proposed = int(round(median * multiplier))
            if proposed > 0:
                proposals[target][cid] = proposed
    return proposals


def apply_to_ynab(client, plan_id: str, month_str: str,
                  category_id: str, budgeted_cents: int) -> None:
    """PATCH a single month-category's budgeted amount."""
    api = ynab.CategoriesApi(client)
    api.update_month_category(
        plan_id=plan_id,
        month=date.fromisoformat(month_str + "-01"),
        category_id=category_id,
        data=ynab.PatchMonthCategoryWrapper(
            category=ynab.SaveMonthCategory(
                budgeted=_milliunits_from_cents(budgeted_cents),
            ),
        ),
    )


def mirror_local(db_path: str, month_str: str, category_id: str,
                 budgeted_cents: int) -> None:
    """UPSERT into local month_category so envelope.recompute_month
    has the same numbers as YNAB."""
    with storage.connect(db_path) as con:
        con.execute(
            """INSERT INTO month_category
               (month, category_id, budgeted_cents, activity_cents, available_cents)
               VALUES (?, ?, ?, 0, ?)
               ON CONFLICT(month, category_id) DO UPDATE SET
                 budgeted_cents = excluded.budgeted_cents""",
            (month_str, category_id, budgeted_cents, budgeted_cents),
        )


def main():
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = load_settings()

    target_months = args.months.split(",")
    learn_months = args.learn_months.split(",")

    cfg = ynab.Configuration(access_token=settings.ynab_token)
    print(f"Learning pattern from: {', '.join(learn_months)}")
    print(f"Target months: {', '.join(target_months)}")
    print(f"Multiplier: {args.multiplier}")
    print()

    with ynab.ApiClient(cfg) as client:
        # Fetch learn months
        print("Fetching prior months from YNAB...")
        learn_data: dict[str, dict[str, dict]] = {}
        for m in learn_months:
            learn_data[m] = fetch_month_budgets(client, settings.ynab.budget_id, m)
            print(f"  {m}: {len(learn_data[m])} categories")

        # Compute proposal
        proposals = compute_proposal(learn_data, target_months, args.multiplier)

        # Apply --exclude filter: drop categories whose name contains any
        # of the given substrings (case-insensitive).
        exclude_terms = [
            s.strip().lower() for s in args.exclude.split(",") if s.strip()
        ]
        if exclude_terms:
            # We need names to filter, so build a category-name lookup
            # from any of the fetched months.
            name_lookup: dict[str, str] = {}
            for m_data in learn_data.values():
                for cid, info in m_data.items():
                    name_lookup.setdefault(cid, info["name"])
            for target in proposals:
                drop = [
                    cid for cid in proposals[target]
                    if any(t in name_lookup.get(cid, "").lower()
                           for t in exclude_terms)
                ]
                for cid in drop:
                    del proposals[target][cid]
            log.info("Excluded %d categories matching %r",
                     len({cid for m in proposals for cid in []}), exclude_terms)

        # Pretty-print
        print()
        any_data = next(iter(learn_data.values()))
        cat_names = {cid: info["name"] for cid, info in any_data.items()}
        # Also include names that may only exist in later months
        for m_data in learn_data.values():
            for cid, info in m_data.items():
                cat_names.setdefault(cid, info["name"])

        # Sort categories by total proposed across all target months
        cat_totals = {
            cid: sum(proposals[m].get(cid, 0) for m in target_months)
            for cid in cat_names
        }
        sorted_cats = sorted(cat_totals.items(), key=lambda x: -x[1])

        print(f"{'category':35s} | " + " | ".join(f"{m[5:]:>8s}" for m in target_months))
        print("-" * (35 + 12 * len(target_months)))
        grand_totals = {m: 0 for m in target_months}
        for cid, _ in sorted_cats:
            row = [f"{cat_names.get(cid, '?')[:33]:35s}"]
            for m in target_months:
                b = proposals[m].get(cid, 0)
                grand_totals[m] += b
                row.append(f"${b/100:>7.2f}" if b > 0 else "      --")
            print(" | ".join(row))
        print("-" * (35 + 12 * len(target_months)))
        row = [f"{'TOTAL':35s}"]
        for m in target_months:
            row.append(f"${grand_totals[m]/100:>7.2f}")
        print(" | ".join(row))

        if not args.apply:
            print()
            print("DRY RUN — pass --apply to write to YNAB + local DB.")
            return 0

        # Apply
        print()
        print("Applying...")
        applied = 0
        for m in target_months:
            for cid, cents in proposals[m].items():
                try:
                    apply_to_ynab(client, settings.ynab.budget_id, m, cid, cents)
                    mirror_local(settings.paths.database, m, cid, cents)
                    applied += 1
                except Exception as e:  # noqa: BLE001
                    log.warning("apply failed for %s/%s: %s", m, cid, e)
        print(f"Applied {applied} budget rows across {len(target_months)} months.")
        storage.audit(settings.paths.database, "apply_budgets_history", {
            "target_months": target_months,
            "learn_months": learn_months,
            "multiplier": args.multiplier,
            "applied": applied,
        })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
