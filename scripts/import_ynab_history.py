"""Bootstrap Phase 2 ledger tables from the YNAB history dump.

Reads `_LOCAL_SECRETS_/ynab_history/{accounts,categories}.json` and
`transactions.csv` and populates `account`, `category_group`, `category`,
`ledger_txn`, and `month_category`.

Classifies categories as non-spending when:
  - their group is "Credit Card Payments" or "Internal Master Category"
  - their name contains a parenthesized day-of-month like "(1st)", "(4th)",
    "(11th)", "(18th)" — those are scheduled bills funded by goals
This makes the ledger's `is_spending=1` filter feed only real spending
categories to the LLM categorizer downstream.

Idempotent — uses INSERT OR REPLACE for accounts/categories/category_groups
and a deterministic ledger key (the YNAB transaction id) to dedupe. Safe
to re-run after `python -m scripts.dump_ynab_history` refreshes the dump.

Usage:
    python -m scripts.import_ynab_history
    python -m scripts.import_ynab_history --since 2024-01-01   # subset
    python -m scripts.import_ynab_history --dry-run
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import re
from collections import defaultdict
from datetime import date
from pathlib import Path

from bot import storage
from bot.config import load_settings

log = logging.getLogger("import_ynab_history")

DUMP = Path("_LOCAL_SECRETS_/ynab_history")

# Groups whose categories are never picked as a spending category.
# These are: CC payoff buckets, the YNAB internal Inflow/Uncategorized pair,
# scheduled-bill envelopes (Monthly/Quarterly/Annual), and savings/investment
# buckets that the user manually assigns to. The LLM should NEVER suggest
# these for spontaneous spending — they're "goals" the user funds explicitly.
NON_SPENDING_GROUPS = {
    "Credit Card Payments",
    "Internal Master Category",
    "Monthly Bills",
    "Quarterly Bills",
    "Annual or Seasonal Costs",
    "Investments",
    "Savings",
    "Individual Vacations",
    "Business Fund",
    "Personal Spending",
}

# Day-of-month pattern in the category name → scheduled bill, not spending
DAY_OF_MONTH_RE = re.compile(r"\((\d{1,2})(?:st|nd|rd|th)\)", re.IGNORECASE)


def _ynab_type_to_local(ynab_type: str) -> str:
    """Strip the SDK's "AccountType." prefix and map to our CHECK values."""
    t = ynab_type.replace("AccountType.", "").lower()
    mapping = {
        "checking": "checking",
        "savings": "savings",
        "creditcard": "credit_card",
        "cash": "cash",
        "lineofcredit": "line_of_credit",
        "otherasset": "other_asset",
        "otherliability": "other_liability",
    }
    return mapping.get(t, "other_asset")


def _classify_category(cat: dict, group_name: str) -> tuple[int, int | None]:
    """Decide is_spending + goal_day from group + name + goal fields.

    Returns (is_spending, goal_day).
    """
    if group_name in NON_SPENDING_GROUPS:
        return 0, None
    m = DAY_OF_MONTH_RE.search(cat["name"])
    if m:
        return 0, int(m.group(1))
    # Otherwise it's a spending category. Goal_kind may still be set (NEED,
    # MF, TBD, etc.) but it doesn't disqualify.
    return 1, None


def _dollars_to_cents(s) -> int:
    """YNAB dump stores dollars as floats; convert to integer cents."""
    if s in (None, ""):
        return 0
    return int(round(float(s) * 100))


def _cleared_to_local(c: str) -> str:
    """Strip the SDK's "TransactionClearedStatus." prefix."""
    if not c:
        return "uncleared"
    t = c.replace("TransactionClearedStatus.", "").lower()
    if t == "reconciled":
        return "reconciled"
    if t == "cleared":
        return "cleared"
    return "uncleared"


def import_accounts(con) -> int:
    data = json.loads((DUMP / "accounts.json").read_text())
    rows = []
    for a in data:
        rows.append(
            (
                a["id"],
                a["name"],
                _ynab_type_to_local(a["type"]),
                1 if a["on_budget"] else 0,
                a["id"],  # ynab_account_id
                1 if a["closed"] else 0,
                _dollars_to_cents(a.get("balance", 0)),
                _dollars_to_cents(a.get("cleared_balance", 0)),
            )
        )
    con.executemany(
        """INSERT OR REPLACE INTO account
           (id, name, type, on_budget, ynab_account_id, closed,
            balance_cents, cleared_balance_cents)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    return len(rows)


def import_categories(con) -> tuple[int, int]:
    data = json.loads((DUMP / "categories.json").read_text())
    # Build groups first
    group_map: dict[str, str] = {}
    for c in data:
        group_map[c["group_id"]] = c["group_name"]
    con.executemany(
        """INSERT OR REPLACE INTO category_group
           (id, name, sort_order, hidden) VALUES (?, ?, 0, 0)""",
        [(gid, gname) for gid, gname in group_map.items()],
    )

    # Then categories
    cat_rows = []
    for c in data:
        is_spending, goal_day = _classify_category(c, c["group_name"])
        goal_target_cents = (
            _dollars_to_cents(c["goal_target"])
            if c.get("goal_target") is not None
            else None
        )
        cat_rows.append(
            (
                c["id"],
                c["group_id"],
                c["name"],
                c["id"],  # ynab_category_id
                1 if c["hidden"] else 0,
                is_spending,
                c.get("goal_type"),
                goal_target_cents,
                goal_day,
            )
        )
    con.executemany(
        """INSERT OR REPLACE INTO category
           (id, group_id, name, ynab_category_id, hidden, is_spending,
            goal_kind, goal_target_cents, goal_day)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        cat_rows,
    )
    return len(group_map), len(cat_rows)


def import_transactions(con, since: date | None = None) -> tuple[int, int, int]:
    """Returns (inserted, skipped, fk_nullified).

    fk_nullified counts transactions referencing categories not in our table
    (deleted/orphaned in YNAB) — we keep the transaction but null its category.
    """
    n_inserted = 0
    n_skipped = 0
    n_fk_nullified = 0
    activity_rollup: dict[tuple[str, str], int] = defaultdict(int)

    # Pre-fetch valid FK targets so we can NULL out orphans rather than crash.
    known_account_ids = {r[0] for r in con.execute("SELECT id FROM account")}
    known_category_ids = {r[0] for r in con.execute("SELECT id FROM category")}

    with open(DUMP / "transactions.csv", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        batch = []
        BATCH_SIZE = 500
        for row in reader:
            if row["deleted"].lower() == "true":
                n_skipped += 1
                continue
            if row["transfer_account_id"]:
                n_skipped += 1
                continue
            posted_date = date.fromisoformat(row["date"])
            if since and posted_date < since:
                n_skipped += 1
                continue
            account_id = row["account_id"]
            if account_id not in known_account_ids:
                # Account was never imported (shouldn't happen post-accounts step)
                n_skipped += 1
                continue

            amount_cents = _dollars_to_cents(row["amount"])
            category_id = row["category_id"] or None
            if category_id and category_id not in known_category_ids:
                category_id = None
                n_fk_nullified += 1
            payee = row["payee_name"] or ""
            month = row["date"][:7]

            if category_id and amount_cents:
                activity_rollup[(month, category_id)] += amount_cents

            batch.append(
                (
                    account_id,
                    posted_date,
                    amount_cents,
                    payee,
                    row["memo"] or "",
                    category_id,
                    _cleared_to_local(row["cleared"]),
                    "ynab_history",
                    row["id"],
                    f"{account_id}|{row['date']}|{abs(amount_cents)}",
                )
            )
            if len(batch) >= BATCH_SIZE:
                _flush_txn_batch(con, batch)
                n_inserted += len(batch)
                batch = []

        if batch:
            _flush_txn_batch(con, batch)
            n_inserted += len(batch)

    # Now write the month_category rollups
    rollup_rows = [
        (month, category_id, activity_cents)
        for (month, category_id), activity_cents in activity_rollup.items()
    ]
    con.executemany(
        """INSERT OR REPLACE INTO month_category
           (month, category_id, budgeted_cents, activity_cents, available_cents)
           VALUES (?, ?, 0, ?, 0)""",
        rollup_rows,
    )
    log.info("month_category rollups written: %d (month, category) pairs",
             len(rollup_rows))

    return n_inserted, n_skipped, n_fk_nullified


def _flush_txn_batch(con, batch) -> None:
    # INSERT OR IGNORE because re-runs would conflict on ynab_txn_id UNIQUE.
    con.executemany(
        """INSERT OR IGNORE INTO ledger_txn
           (account_id, posted_date, amount_cents, payee, memo,
            category_id, cleared, source_signal, ynab_txn_id, dedupe_key)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        batch,
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--since", type=date.fromisoformat, default=None,
                   help="only import transactions on or after this date (YYYY-MM-DD)")
    p.add_argument("--dry-run", action="store_true",
                   help="parse + classify but don't write")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not DUMP.exists():
        log.error("YNAB dump not found at %s — run `python -m scripts.dump_ynab_history` first", DUMP)
        return 1

    settings = load_settings()
    storage.init_db(settings.paths.database)

    if args.dry_run:
        log.info("DRY RUN — no DB writes")
        return 0

    # Commit accounts + categories first so a transaction-side failure can't
    # roll them back. Each step is its own connection / transaction.
    with storage.connect(settings.paths.database) as con:
        n_acct = import_accounts(con)
    log.info("accounts: %d", n_acct)

    with storage.connect(settings.paths.database) as con:
        n_groups, n_cats = import_categories(con)
        non_spending = con.execute(
            "SELECT COUNT(*) FROM category WHERE is_spending = 0"
        ).fetchone()[0]
    log.info("category_groups: %d", n_groups)
    log.info("categories: %d (marked is_spending=0: %d — CC payments + scheduled bills)",
             n_cats, non_spending)

    with storage.connect(settings.paths.database) as con:
        n_inserted, n_skipped, n_fk_nullified = import_transactions(
            con, since=args.since
        )
    log.info("ledger_txn inserted: %d, skipped: %d (transfers / deletes), "
             "category_id nullified: %d (orphaned/deleted in YNAB)",
             n_inserted, n_skipped, n_fk_nullified)

    # Audit
    storage.audit(settings.paths.database, "ynab_history_import",
                  {"accounts": n_acct, "categories": n_cats,
                   "ledger_txn_inserted": n_inserted,
                   "fk_nullified": n_fk_nullified,
                   "since": str(args.since) if args.since else None})
    log.info("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
