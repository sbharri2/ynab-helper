"""Enrich Amazon transactions with item details from the offline order dump.

Reads `_LOCAL_SECRETS_/amazon_history/orders.json` (manually pasted from
amazon.com order history pages for both Steven's and Allison's accounts)
and joins it onto:

  - pending_txn: where status='pending' and payee like 'Amazon%'.
    Updates raw_summary with the item names AND re-runs the hardened
    Categorizer with priors, so the bot can DM a real best-guess.
  - ledger_txn: where source_signal='ynab_history' and payee like 'Amazon%'
    and memo is blank. Sets memo so future agent drill-downs have
    real context.

Matching rule:
  - amount_cents must match exactly (Amazon order total = CC charge).
  - txn_date must be in [order_date, order_date + match_window_days].
    Posting lag of 0-5 days is the observed norm.

Idempotent — re-running is safe; rows that already carry a summary/memo
matching the order get re-asserted but not double-suggested.

Usage:
    python -m scripts.enrich_amazon_orders                  # default
    python -m scripts.enrich_amazon_orders --dry-run         # preview only
    python -m scripts.enrich_amazon_orders --skip-ledger     # only pending
    python -m scripts.enrich_amazon_orders --skip-pending    # only history
    python -m scripts.enrich_amazon_orders --window-days 7   # widen match
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import date, timedelta
from pathlib import Path

from bot import storage
from bot.categorizer import Categorizer
from bot.config import load_settings

log = logging.getLogger("enrich_amazon")

DUMP = Path("_LOCAL_SECRETS_/amazon_history/orders.json")


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--skip-ledger", action="store_true",
                   help="skip backfilling ledger_txn.memo for historical rows")
    p.add_argument("--skip-pending", action="store_true",
                   help="skip enriching pending_txn / re-running categorizer")
    p.add_argument("--window-days", type=int, default=5,
                   help="match txn_date within [order_date, order_date+N]")
    return p.parse_args()


def _load_orders() -> list[dict]:
    if not DUMP.exists():
        raise SystemExit(
            f"Amazon order dump not found at {DUMP}. "
            f"Paste order data into that file first."
        )
    data = json.loads(DUMP.read_text(encoding="utf-8"))
    out = []
    for o in data["orders"]:
        out.append({
            "account": o["account"],
            "order_date": date.fromisoformat(o["date"]),
            "order_id": o["order_id"],
            "total_cents": int(o["total_cents"]),
            "items": o["items"],
            "kind": o.get("kind", "physical"),
        })
    return out


def _match(orders: list[dict], *, amount_cents: int, txn_date: date,
           window_days: int) -> dict | None:
    """Return the single matching order, or None if 0/many."""
    target_amt = abs(int(amount_cents))
    cands = []
    for o in orders:
        if o["total_cents"] != target_amt:
            continue
        delta = (txn_date - o["order_date"]).days
        if 0 <= delta <= window_days:
            cands.append((delta, o))
    if not cands:
        return None
    cands.sort(key=lambda x: x[0])
    return cands[0][1]


def enrich_pending(db_path: str, orders: list[dict], cat_engine: Categorizer,
                   categories: list[dict], *, window_days: int,
                   dry_run: bool) -> tuple[int, int, int]:
    """Returns (matched, suggested, skipped_no_match)."""
    matched = 0
    suggested = 0
    skipped_no_match = 0

    with storage.connect(db_path) as con:
        rows = con.execute(
            """SELECT id, txn_date, amount_cents, payee, raw_summary,
                      suggested_category
               FROM pending_txn
               WHERE status='pending' AND payee LIKE 'Amazon%'
               ORDER BY txn_date"""
        ).fetchall()
        rows = [dict(r) for r in rows]

    log.info("pending Amazon rows: %d", len(rows))

    for r in rows:
        match = _match(orders, amount_cents=r["amount_cents"],
                       txn_date=r["txn_date"], window_days=window_days)
        if not match:
            skipped_no_match += 1
            log.info("  no-match  id=%d %s $%.2f", r["id"], r["txn_date"],
                     r["amount_cents"] / 100)
            continue

        matched += 1
        new_summary = f"Amazon order {match['order_id']} ({match['account']}): {match['items']}"

        log.info("  match     id=%d %s $%.2f -> %s",
                 r["id"], r["txn_date"], r["amount_cents"] / 100,
                 match["items"][:60])

        # Re-run categorizer with the enriched summary + priors.
        priors = storage.get_category_priors_for_payee(
            db_path, r["payee"], top_n=5,
        )
        ts = time.monotonic()
        result = cat_engine.suggest(
            summary=new_summary,
            amount_cents=r["amount_cents"],
            date_str=str(r["txn_date"]),
            source="amazon",
            categories=categories,
            priors=priors,
        )
        latency_ms = int((time.monotonic() - ts) * 1000)

        cat_id = result.get("category_id")
        cat_name = next((c["name"] for c in categories if c["id"] == cat_id),
                        "(none)")
        conf = float(result.get("confidence") or 0)
        log.info("            -> %s (conf=%.2f, %dms)",
                 cat_name, conf, latency_ms)

        if not dry_run:
            with storage.connect(db_path) as con:
                if cat_id:
                    con.execute(
                        "UPDATE pending_txn SET raw_summary=?, "
                        "suggested_category=? WHERE id=?",
                        (new_summary, cat_id, r["id"]),
                    )
                    suggested += 1
                else:
                    con.execute(
                        "UPDATE pending_txn SET raw_summary=? WHERE id=?",
                        (new_summary, r["id"]),
                    )

    return matched, suggested, skipped_no_match


def enrich_ledger(db_path: str, orders: list[dict], *, window_days: int,
                  dry_run: bool) -> tuple[int, int]:
    """Returns (matched, skipped). Only updates rows where memo is blank."""
    matched = 0
    skipped = 0

    with storage.connect(db_path) as con:
        rows = con.execute(
            """SELECT id, posted_date, amount_cents, payee, memo
               FROM ledger_txn
               WHERE source_signal='ynab_history'
                 AND payee LIKE 'Amazon%'
                 AND (memo IS NULL OR memo='')
                 AND posted_date >= '2026-01-01'
               ORDER BY posted_date"""
        ).fetchall()
        rows = [dict(r) for r in rows]

    log.info("ledger_txn rows missing memo: %d (2026+)", len(rows))

    updates = []
    for r in rows:
        match = _match(orders, amount_cents=r["amount_cents"],
                       txn_date=date.fromisoformat(str(r["posted_date"])),
                       window_days=window_days)
        if not match:
            skipped += 1
            continue
        matched += 1
        memo = f"order {match['order_id']} ({match['account']}): {match['items']}"
        updates.append((memo[:500], r["id"]))

    if updates and not dry_run:
        with storage.connect(db_path) as con:
            con.executemany(
                "UPDATE ledger_txn SET memo=? WHERE id=?", updates,
            )

    return matched, skipped


def main():
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = load_settings()

    orders = _load_orders()
    log.info("loaded %d Amazon orders from %s", len(orders), DUMP)

    cat_engine = Categorizer(
        settings.ollama.endpoint, settings.ollama.model,
        settings.ollama.temperature,
    )
    spending_cats = storage.list_categories_for_spending(settings.paths.database)
    categories = [
        {"id": c["id"], "name": c["name"], "group": c["group_name"]}
        for c in spending_cats
    ]
    log.info("%d spending categories available to LLM", len(categories))

    summary = {"orders_loaded": len(orders),
               "window_days": args.window_days,
               "dry_run": args.dry_run}

    if not args.skip_pending:
        log.info("=" * 60)
        log.info("ENRICHING pending_txn ...")
        m, s, n = enrich_pending(
            settings.paths.database, orders, cat_engine, categories,
            window_days=args.window_days, dry_run=args.dry_run,
        )
        summary["pending_matched"] = m
        summary["pending_suggested"] = s
        summary["pending_no_match"] = n
        log.info("pending: %d matched / %d got new suggestion / %d no-match",
                 m, s, n)

    if not args.skip_ledger:
        log.info("=" * 60)
        log.info("BACKFILLING ledger_txn memos ...")
        m, s = enrich_ledger(
            settings.paths.database, orders,
            window_days=args.window_days, dry_run=args.dry_run,
        )
        summary["ledger_matched"] = m
        summary["ledger_no_match"] = s
        log.info("ledger: %d matched / %d no-match", m, s)

    storage.audit(settings.paths.database, "enrich_amazon_orders", summary)
    log.info("DONE: %s", summary)


if __name__ == "__main__":
    main()
