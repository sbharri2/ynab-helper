"""Enrich pending + historical transactions with retailer order details.

Mirror of `scripts/enrich_amazon_orders.py` but for the Shopify-templated
retailers (Rhoback, Larke, Zappos, Urban Outfitters, Stripe-receipted
events, Bookshop, Chatham Rabbits, Barnes & Noble, Etsy, etc.) backfilled
into `_LOCAL_SECRETS_/retailer_history/orders.json` by the sub-agent.

What it does:

  * `pending_txn` rows where status='pending' and payee matches one of
    the merchants — fills `raw_summary` with the items + re-runs the
    hardened Categorizer (with priors) so the bot has a real best guess.
  * `ledger_txn` rows with `source_signal='ynab_history'` from the same
    merchants and missing `memo` — backfills the memo so future agent
    drill-downs see the item details.

Matching: exact `abs(amount_cents)` and `posted_date` within
``[order_date, order_date + window]`` days (CC posting lag is typically
0-5 days). Retailer payees vary (e.g. "Sp Rhoback Apparel", "Urban
Outfit", "Barnes & Noble #1234"), so we use a substring/lowercase
match against each order's merchant tokens — see `_payee_matches`.

Usage:
    python -m scripts.enrich_retailer_orders                 # default
    python -m scripts.enrich_retailer_orders --dry-run        # preview
    python -m scripts.enrich_retailer_orders --skip-ledger    # only pending
    python -m scripts.enrich_retailer_orders --window-days 7  # widen match
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import date
from pathlib import Path

from bot import storage
from bot.categorizer import Categorizer
from bot.config import load_settings

log = logging.getLogger("enrich_retailer")

DUMP = Path("_LOCAL_SECRETS_/retailer_history/orders.json")

# Substrings (lowercase) we try to find inside the YNAB payee for each
# merchant. Banks abbreviate aggressively ("Urban Outfit", "Sp Rhoback
# Apparel", "Barnes Noble"), so each merchant has a small set of tokens
# we accept. The first matching token wins.
MERCHANT_PAYEE_TOKENS = {
    "Rhoback":              ["rhoback"],
    "Larke":                ["larke", "shoplarke"],
    "Zappos":               ["zappos"],
    "Urban Outfitters":     ["urban outfit", "urbanout", "urban out"],
    "Capstone Event Group": ["capstone", "city of oaks", "stripe"],
    "Bookshop.org":         ["bookshop"],
    "Chatham Rabbits":      ["chatham rabbit", "chathamrabbits"],
    "Barnes & Noble":       ["barnes", "b&n", "bn.com"],
    "Etsy":                 ["etsy"],
}


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--skip-ledger", action="store_true")
    p.add_argument("--skip-pending", action="store_true")
    p.add_argument("--window-days", type=int, default=5,
                   help="match txn_date within [order_date, order_date+N]")
    return p.parse_args()


def _load_orders() -> list[dict]:
    if not DUMP.exists():
        raise SystemExit(
            f"Retailer order dump not found at {DUMP}. "
            f"The sub-agent should have written it; check the file."
        )
    data = json.loads(DUMP.read_text(encoding="utf-8"))
    out = []
    for o in data["orders"]:
        out.append({
            "merchant": o["merchant"],
            "order_date": date.fromisoformat(o["date"]),
            "order_id": o["order_id"],
            "total_cents": int(o["total_cents"]),
            "items": o["items"],
        })
    return out


def _payee_matches(payee: str, merchant: str) -> bool:
    """Does the bank/YNAB payee string match this merchant?"""
    if not payee:
        return False
    p = payee.lower()
    tokens = MERCHANT_PAYEE_TOKENS.get(merchant, [merchant.lower()])
    return any(t in p for t in tokens)


def _match(orders: list[dict], *, amount_cents: int, txn_date: date,
           window_days: int, payee: str | None = None) -> dict | None:
    target_amt = abs(int(amount_cents))
    cands = []
    for o in orders:
        if o["total_cents"] != target_amt:
            continue
        delta = (txn_date - o["order_date"]).days
        if 0 <= delta <= window_days:
            # If we have a payee, require the merchant matches too — this
            # avoids cross-merchant amount collisions (e.g. $30 at Etsy
            # vs $30 at Barnes & Noble in the same week).
            if payee and not _payee_matches(payee, o["merchant"]):
                continue
            cands.append((delta, o))
    if not cands:
        return None
    cands.sort(key=lambda x: x[0])
    return cands[0][1]


def enrich_pending(db_path: str, orders: list[dict], cat_engine: Categorizer,
                   categories: list[dict], *, window_days: int,
                   dry_run: bool) -> tuple[int, int, int]:
    matched = 0
    suggested = 0
    skipped_no_match = 0

    # Pull all pending txns whose payee plausibly matches one of our merchants.
    merchant_likes = " OR ".join(
        "LOWER(payee) LIKE ?" for tokens in MERCHANT_PAYEE_TOKENS.values()
        for _ in tokens
    )
    bind = []
    for tokens in MERCHANT_PAYEE_TOKENS.values():
        for t in tokens:
            bind.append(f"%{t}%")
    with storage.connect(db_path) as con:
        rows = con.execute(
            f"""SELECT id, txn_date, amount_cents, payee, raw_summary,
                       suggested_category
                FROM pending_txn
                WHERE status='pending' AND ({merchant_likes})
                ORDER BY txn_date""",
            bind,
        ).fetchall()
        rows = [dict(r) for r in rows]

    log.info("pending retailer rows: %d", len(rows))

    for r in rows:
        match = _match(orders, amount_cents=r["amount_cents"],
                       txn_date=r["txn_date"], window_days=window_days,
                       payee=r["payee"])
        if not match:
            skipped_no_match += 1
            log.info("  no-match  id=%d %s $%.2f %s",
                     r["id"], r["txn_date"], r["amount_cents"] / 100,
                     (r["payee"] or "")[:25])
            continue

        matched += 1
        new_summary = (
            f"{match['merchant']} order {match['order_id']}: {match['items']}"
        )
        log.info("  match     id=%d %s $%.2f -> %s",
                 r["id"], r["txn_date"], r["amount_cents"] / 100,
                 match["merchant"])

        priors = storage.get_category_priors_for_payee(
            db_path, r["payee"], top_n=5,
        )
        ts = time.monotonic()
        result = cat_engine.suggest(
            summary=new_summary,
            amount_cents=r["amount_cents"],
            date_str=str(r["txn_date"]),
            source="retailer_order",
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
    """Backfill memo on historical ledger_txn rows missing one."""
    merchant_likes = " OR ".join(
        "LOWER(payee) LIKE ?" for tokens in MERCHANT_PAYEE_TOKENS.values()
        for _ in tokens
    )
    bind = []
    for tokens in MERCHANT_PAYEE_TOKENS.values():
        for t in tokens:
            bind.append(f"%{t}%")
    with storage.connect(db_path) as con:
        rows = con.execute(
            f"""SELECT id, posted_date, amount_cents, payee, memo
                FROM ledger_txn
                WHERE source_signal='ynab_history'
                  AND (memo IS NULL OR memo = '')
                  AND ({merchant_likes})
                ORDER BY posted_date""",
            bind,
        ).fetchall()
        rows = [dict(r) for r in rows]

    log.info("ledger rows missing memo (retailer): %d", len(rows))

    updates = []
    skipped = 0
    for r in rows:
        match = _match(orders, amount_cents=r["amount_cents"],
                       txn_date=date.fromisoformat(str(r["posted_date"])),
                       window_days=window_days, payee=r["payee"])
        if not match:
            skipped += 1
            continue
        memo = (
            f"{match['merchant']} order {match['order_id']}: {match['items']}"
        )
        updates.append((memo[:500], r["id"]))

    if updates and not dry_run:
        with storage.connect(db_path) as con:
            con.executemany("UPDATE ledger_txn SET memo=? WHERE id=?", updates)

    return len(updates), skipped


def main():
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = load_settings()

    orders = _load_orders()
    log.info("loaded %d retailer orders from %s", len(orders), DUMP)

    cat_engine = Categorizer(
        settings.ollama.endpoint, settings.ollama.model,
        settings.ollama.temperature,
    )
    spending = storage.list_categories_for_spending(settings.paths.database)
    categories = [
        {"id": c["id"], "name": c["name"], "group": c["group_name"]}
        for c in spending
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

    storage.audit(settings.paths.database, "enrich_retailer_orders", summary)
    log.info("DONE: %s", summary)


if __name__ == "__main__":
    main()
