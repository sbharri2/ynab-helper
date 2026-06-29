"""One-shot: zero Alternate Investment budget for Mar-Jun 2026 in YNAB.

Local mirror has already been updated. This script just pushes to YNAB
once the rate limit window resets. Safe to retry — idempotent.
"""
from __future__ import annotations

from datetime import date

import ynab

from bot.config import load_settings

ALT_INV_ID = "87e3de44-a880-4ede-bd16-e7dacd69db7f"
MONTHS = ["2026-03", "2026-04", "2026-05", "2026-06"]


def main():
    settings = load_settings()
    cfg = ynab.Configuration(access_token=settings.ynab_token)
    with ynab.ApiClient(cfg) as client:
        api = ynab.CategoriesApi(client)
        for m_str in MONTHS:
            try:
                api.update_month_category(
                    plan_id=settings.ynab.budget_id,
                    month=date.fromisoformat(m_str + "-01"),
                    category_id=ALT_INV_ID,
                    data=ynab.PatchMonthCategoryWrapper(
                        category=ynab.SaveMonthCategory(budgeted=0),
                    ),
                )
                print(f"{m_str}: zeroed in YNAB")
            except ynab.ApiException as e:
                if e.status == 429:
                    print(f"{m_str}: rate-limited (429). Wait and retry.")
                    return 1
                raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
