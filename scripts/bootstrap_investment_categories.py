"""Create bot-local Investment/Savings-Transfer categories + payee overrides.

Per Steven's 2026-06-26 directive: money going to Schwab/Coinbase/etc is
SAVING not spending. This script:

  1. Creates a bot-local category group "Investments / Savings Transfers"
     with is_spending=0 if it doesn't exist.
  2. Creates one sub-category per investment destination, mirroring the
     accounts in the spreadsheet.
  3. Returns the (group_id, category_id-by-name) map so the overrides
     module can reference them.

Idempotent: re-running adds only missing categories.

These categories are NOT pushed to YNAB. The bot's ynab_writer skips
rows whose chosen_category is in a non-spending group, OR — more
practically — we just never invoke writer.run_once() for these category
ids. They stay local-only.
"""
from __future__ import annotations

import sys
import uuid

from bot import storage
from bot.config import load_settings


GROUP_NAME = "Investments / Savings Transfers"

# (display_name, account_number_substring_for_uniqueness — optional)
# The display names mirror the spreadsheet's account labels. Keep them
# distinct so the bot's overrides map cleanly.
INVESTMENT_CATEGORIES = [
    "Marcus Online Bank",
    "Coastal Federal Credit Union",
    "Treasury Direct (Bonds)",
    "Treasury Direct (T-Bills)",
    "Coinbase",
    "Bitcoin Wallet",
    "Ethereum Wallet",
    "Vanguard - Steven Roth IRA",
    "Vanguard - Luke 529",
    "Vanguard - Josie 529",
    "Fidelity 401K",
    "Principal Financial 401K",
    "Principal Financial ESOP",
    "Guideline 401K",
    "American Funds SIMPLE IRA",
    "American Funds 401K",
    "OBA Profit Sharing",
    "OBA Stock",
    "TD Ameritrade Roth IRA",
    "TD Ameritrade Stock",
    "Schwab Roth IRA",
    "Schwab Stock Account",
    "Health Equity HSA",
    "Nationwide 401K",
]


def main() -> int:
    settings = load_settings()
    db_path = settings.paths.database

    with storage.connect(db_path) as con:
        # Find or create the group.
        existing_group = con.execute(
            "SELECT id FROM category_group WHERE name = ?",
            (GROUP_NAME,),
        ).fetchone()
        if existing_group:
            group_id = existing_group["id"]
            print(f"group exists: {GROUP_NAME}  id={group_id}")
        else:
            group_id = f"botgrp_{uuid.uuid4().hex[:12]}"
            max_sort = con.execute(
                "SELECT COALESCE(MAX(sort_order), 0) AS m FROM category_group"
            ).fetchone()["m"]
            con.execute(
                "INSERT INTO category_group (id, name, sort_order, hidden) "
                "VALUES (?, ?, ?, 0)",
                (group_id, GROUP_NAME, max_sort + 1),
            )
            print(f"created group: {GROUP_NAME}  id={group_id}")

        # Find or create each category.
        added = 0
        existing = 0
        for cat_name in INVESTMENT_CATEGORIES:
            row = con.execute(
                "SELECT id FROM category WHERE group_id = ? AND name = ?",
                (group_id, cat_name),
            ).fetchone()
            if row:
                existing += 1
                continue
            cat_id = f"botcat_{uuid.uuid4().hex[:12]}"
            con.execute(
                "INSERT INTO category "
                "(id, group_id, name, hidden, is_spending) "
                "VALUES (?, ?, ?, 0, 0)",
                (cat_id, group_id, cat_name),
            )
            added += 1
            print(f"  + {cat_name}")
        print(f"\nadded {added} categories, {existing} already existed")

    storage.audit(db_path, "investment_categories_bootstrapped", {
        "group_name": GROUP_NAME,
        "added": added,
        "existing": existing,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
