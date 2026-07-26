"""Raise already-filed Amazon charges at/above the large-charge threshold.

Clears ledger_txn.category_id, enqueues a COLD pending_txn so the row shows
up in the Inbox, and refreshes ONLY the affected bucket categories' cached
month_category rows.

Uses apply_activity_delta (additive, anchor-preserving, scoped to the given
categories) not recompute_month — the latter rebuilds `available` from the
identity and trampled same-month anchor writes on 2026-07-25 (see
bot/envelope.py:301).

Each row's pending_txn insert, ledger mutation, audit entry, AND its
month_category refresh happen together, one row at a time — the refresh is
NOT batched until after the whole loop, and the pending_txn is created
BEFORE ledger_txn.category_id is cleared. This ordering matters: clearing
category_id is what makes a row invisible to this script's own SELECT (it
joins on category), so it is deliberately the LAST write for a given row.
If a failure (e.g. a concurrent bot holding a lock) hits anywhere before
that point, the row is untouched and a plain re-run selects it again
normally. If it hits after, the row is already fully consistent
(pending_txn in place, uncategorized, refreshed) — never stranded.

A `--expect` guard (default 2, the known target count for this repair)
aborts before any write if the matched row count differs from what's
expected, so a later widened `--since` can't silently sweep and re-open
unintended historical charges. Pass --allow-unexpected-count to knowingly
run a wider sweep.

Usage:
    .venv/Scripts/python.exe scripts/raise_large_amazon_charges.py --dry-run
    .venv/Scripts/python.exe scripts/raise_large_amazon_charges.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-06-01",
                    help="only touch charges posted on/after this date")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--expect", type=int, default=2,
                    help="required number of matching charges; abort "
                         "before touching the database if the actual "
                         "count differs (default 2, the known target set "
                         "for this repair). See --allow-unexpected-count "
                         "to bypass for a deliberate wider sweep.")
    ap.add_argument("--allow-unexpected-count", action="store_true",
                    help="bypass the --expect guard (deliberate wider sweep)")
    args = ap.parse_args()

    from dotenv import load_dotenv
    repo = Path(__file__).resolve().parents[1]
    load_dotenv(repo / ".env", override=True)

    from bot import envelope, ingest, storage
    from bot.config import load_settings

    settings = load_settings(repo / "config.yaml")
    db = settings.paths.database

    with storage.connect(db) as con:
        rows = [dict(r) for r in con.execute(
            """SELECT lt.id, lt.posted_date, lt.payee, lt.amount_cents,
                      lt.account_id, lt.category_id, c.name AS cat_name
               FROM ledger_txn lt
               JOIN category c ON c.id = lt.category_id
               WHERE c.name LIKE 'Amazon - %'
                 AND lt.posted_date >= ?
                 AND lt.is_split = 0""",
            (args.since,),
        )]

    targets = [r for r in rows
               if ingest._is_large_amazon_charge(r["amount_cents"], settings)]
    if not targets:
        print("nothing to raise.")
        return 0

    # A dry run performs no writes, so the --expect guard buys nothing
    # there — worse, gating it in dry-run mode broke its own advice: the
    # message told you to "re-run with --dry-run to inspect the set", but
    # the guard fired in dry-run too, so that advice could never work.
    # Only gate real (write) runs.
    if (not args.dry_run and not args.allow_unexpected_count
            and len(targets) != args.expect):
        print(f"ABORT: expected exactly {args.expect} matching charge(s), "
              f"found {len(targets)}. Refusing to touch the database.\n"
              f"Re-run with --dry-run to inspect the actual set, or pass "
              f"--allow-unexpected-count for a deliberate wider sweep.",
              file=sys.stderr)
        return 1

    touched_categories: set[tuple[str, str]] = set()
    for r in targets:
        month = str(r["posted_date"])[:7]
        print(f"  lt#{r['id']}  {r['posted_date']}  "
              f"${r['amount_cents'] / 100:>10,.2f}  {r['payee']}  "
              f"[{r['cat_name']}] -> Inbox")
        touched_categories.add((month, r["category_id"]))
        if args.dry_run:
            continue

        # Order matters for recoverability: create the pending_txn (the
        # Inbox entry) BEFORE clearing ledger_txn.category_id (the step
        # that removes this row from the SELECT's join and makes it
        # un-recoverable by a plain re-run). If insert_pending_txn or the
        # queue_lane update fails, the ledger row is untouched — still
        # categorized, still selected again next run.
        pt_id = storage.insert_pending_txn(
            db,
            user_id="steven",
            ynab_txn_id=f"ledger:{r['id']}",
            ynab_account_id=r["account_id"],
            payee=r["payee"],
            amount_cents=r["amount_cents"],
            txn_date=r["posted_date"],
            memo=f"raised for categorization (was {r['cat_name']})",
        )
        if pt_id is None:
            # insert_pending_txn returns None if ynab_txn_id already
            # exists — a prior partial run got this far. Look the row up
            # so the queue_lane fix-up below still applies idempotently.
            with storage.connect(db) as con:
                existing = con.execute(
                    "SELECT id FROM pending_txn WHERE ynab_txn_id = ?",
                    (f"ledger:{r['id']}",),
                ).fetchone()
            pt_id = existing["id"] if existing else None
        if pt_id:
            with storage.connect(db) as con:
                con.execute(
                    "UPDATE pending_txn SET queue_lane = 'cold' WHERE id = ?",
                    (pt_id,),
                )

        with storage.connect(db) as con:
            con.execute(
                "UPDATE ledger_txn SET category_id = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (r["id"],),
            )
        storage.audit(db, "large_amazon_raised", {
            "ledger_txn_id": r["id"], "pending_txn_id": pt_id,
            "was_category": r["cat_name"], "amount_cents": r["amount_cents"],
        })
        # Refresh THIS row's category right away — not batched until after
        # the loop — so a failure on a later row can never strand an
        # already-mutated row with a stale month_category cache.
        envelope.apply_activity_delta(db, month, [r["category_id"]])
        print(f"  refreshed {month}: {r['cat_name']}")

    if args.dry_run:
        print(f"\n[dry-run] would raise {len(targets)} charge(s); "
              f"would refresh {len(touched_categories)} month/category pair(s).")
        return 0

    print(f"\nraised {len(targets)} charge(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
