"""One-shot backfill: populate split child rows for existing ledger_txn
split parents.

Until 2026-06-28 a YNAB split transaction was stored as a single parent
row with category_id=NULL — so it looked uncategorized and its spend was
invisible to per-category math. ``bot.ynab_full_sync`` now mirrors each
split's subtransactions as child rows. This script forces a full-history
sync so existing split parents (years of them) get their children.

It is safe to re-run (the sync upserts). The daily 6h full-sync would
eventually repopulate recently-edited splits, but only within its 7-day
overlap window — this reaches all the way back.

Usage (PAUSE THE BOT FIRST to avoid SQLite lock contention):
    python -m scripts.backfill_split_children
    python -m scripts.backfill_split_children --since 2020-01-01
    python -m scripts.backfill_split_children --db /path/to/copy.db   # dry test
"""
from __future__ import annotations

import argparse
import logging
from datetime import date

from bot import storage
from bot.config import load_settings
from bot.ynab_full_sync import full_sync


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--since", type=date.fromisoformat, default=date(2000, 1, 1),
                   help="Lower bound for the YNAB pull (default: all history)")
    p.add_argument("--db", default=None,
                   help="Override the DB path (point at a copy to test safely)")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    settings = load_settings()
    if args.db:
        settings.paths.database = args.db
    print(f"DB: {settings.paths.database}")

    # Apply the additive migration (adds parent_txn_id / is_split if absent).
    storage.init_db(settings.paths.database)

    summary = full_sync(settings, since_date=args.since)
    print("full_sync summary:")
    for k, v in summary.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
