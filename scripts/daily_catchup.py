"""Daily catch-up: add LLM suggestions to N pending_txn rows so the bot DMs
them in the daily-digest window.

How it fits in:
  - ynab_watcher enqueues uncategorized YNAB transactions into pending_txn with
    no suggestion. Those rows are NOT eligible for DM (next_item_for_user
    requires suggested_category IS NOT NULL).
  - This script picks the N oldest such rows, enriches Amazon/Venmo with the
    matching email's parsed summary (when available), runs the LLM, and
    writes suggested_category back. Bot then picks them up.

Scheduled to run once per day. --limit controls the daily quota (default 20).
"""
from __future__ import annotations

import argparse
import logging
import re
import time
from datetime import date, timedelta
from pathlib import Path

from bot import storage
from bot.categorizer import Categorizer
from bot.config import load_settings
from bot.gmail_watcher import _build_gmail_service, _extract_body, _load_parser
from bot.matcher import find_best_match

log = logging.getLogger("catchup")

# Payees where the name alone tells you nothing about what was bought.
# These need either an email match (Amazon order, Venmo memo) or a useful
# YNAB memo before we'll DM the user. Other payees (Jersey Mike's, Royal
# Farms, etc.) are specific enough that payee + amount is sufficient context.
_GENERIC_PAYEE_RE = re.compile(r"^\s*(amazon|amzn|venmo|paypal)\b", re.I)


def _useful_memo(memo: str, payee: str) -> bool:
    """A memo is useful only if it adds info beyond the payee name."""
    m = (memo or "").strip().lower()
    p = (payee or "").strip().lower()
    return bool(m) and m != p


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=20,
                   help="Max rows to add suggestions to in one run (default 20)")
    p.add_argument("--since", type=date.fromisoformat, default=None,
                   help="Lower bound for Gmail scrape. Defaults to "
                        "min(txn_date) of un-suggested pending_txn rows")
    p.add_argument("--dry-run", action="store_true",
                   help="Compute suggestions but don't write to DB")
    p.add_argument("--no-skip", action="store_true",
                   help="Disable the generic-payee skip rule (bulk-reset mode: "
                        "let the LLM guess on Amazon/Venmo/etc even without context)")
    return p.parse_args()


def fetch_email_ids(svc, query: str, since_d: date) -> list[dict]:
    """Paginated Gmail message-id list for `query` filtered to after since_d."""
    full_query = f"{query} after:{since_d.strftime('%Y/%m/%d')}"
    out, page_token = [], None
    while True:
        resp = svc.users().messages().list(
            userId="me", q=full_query, maxResults=100, pageToken=page_token,
        ).execute()
        out.extend(resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


def scrape_orders(settings, since_d: date) -> list[dict]:
    """Returns parsed pending-order-shaped dicts from Amazon + Venmo emails since."""
    orders = []
    for account in settings.gmail_accounts:
        svc = _build_gmail_service(account.token_path)
        for source in settings.email_sources:
            base_query = re.sub(r"\s*newer_than:\S+\s*", " ", source.query).strip()
            log.info("scraping %s from %s ...", source.name, account.email)
            ids = fetch_email_ids(svc, base_query, since_d)
            log.info("  found %d %s emails", len(ids), source.name)
            parser = _load_parser(source.parser)
            for m in ids:
                msg = svc.users().messages().get(
                    userId="me", id=m["id"], format="full",
                ).execute()
                body = _extract_body(msg)
                headers = {h["name"]: h["value"]
                           for h in msg["payload"].get("headers", [])}
                parsed = parser(
                    body,
                    subject=headers.get("Subject", ""),
                    date_header=headers.get("Date", ""),
                )
                if parsed["parse_status"] not in {"ok", "partial"}:
                    continue
                orders.append({
                    "source": parsed["source"],
                    "parser": source.parser,
                    "external_id": parsed.get("order_id")
                                   or parsed.get("counterparty") or "",
                    "order_date": parsed.get("order_date") or date.today(),
                    "total_cents": parsed.get("total_cents")
                                   or parsed.get("amount_cents") or 0,
                    "summary": parsed.get("summary", ""),
                })
    return _dedupe_amazon_orders(orders)


def _dedupe_amazon_orders(orders: list[dict]) -> list[dict]:
    """Drop auto-confirm Amazon entries when shipment-tracking entries
    exist for the same order_id.

    Auto-confirm gives the order total; shipment-tracking gives the
    per-shipment total - which is what Amazon actually charges. Keeping
    both creates duplicate candidates that the matcher's ambiguity guard
    then rejects together. Once any shipment exists for an order, prefer
    those exclusively (multi-shipment orders preserved naturally because
    each shipment carries the same order_id but a different total_cents).
    """
    shipped_ids = {
        o["external_id"]
        for o in orders
        if o.get("parser") == "amazon_shipment" and o.get("external_id")
    }
    return [
        o for o in orders
        if not (o.get("parser") == "amazon" and o.get("external_id") in shipped_ids)
    ]


def pick_rows(db_path: str, limit: int) -> list[dict]:
    """Oldest pending_txn rows without a suggestion yet."""
    with storage.connect(db_path) as con:
        rows = con.execute(
            """SELECT * FROM pending_txn
               WHERE status = 'pending' AND suggested_category IS NULL
               ORDER BY txn_date ASC, id ASC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def main():
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    settings = load_settings()
    cat_engine = Categorizer(
        settings.ollama.endpoint, settings.ollama.model, settings.ollama.temperature,
    )

    from bot.ynab_client import YnabClient
    ynab = YnabClient(settings.ynab_token, settings.ynab.budget_id)
    log.info("fetching YNAB categories ...")

    # Phase 3.2: prefer the local spending-only category list (excludes CC
    # payments + scheduled-bill goals). Falls back to YNAB if local ledger
    # hasn't been populated yet.
    spending_cats = storage.list_categories_for_spending(settings.paths.database)
    if spending_cats:
        categories = [
            {"id": c["id"], "name": c["name"], "group": c["group_name"]}
            for c in spending_cats
        ]
        log.info("  %d spending categories (from local ledger)", len(categories))
    else:
        categories = ynab.list_categories()
        log.info("  %d categories (from YNAB; local ledger empty)", len(categories))

    # Pull a wider pool than args.limit - we'll skip rows without enough
    # context to ask the user about, then count only the suggestion-worthy
    # ones toward the daily cap.
    candidate_rows = pick_rows(settings.paths.database, args.limit * 5)
    if not candidate_rows:
        log.info("nothing to do - no pending_txn rows without suggestions")
        return
    log.info("scanning %d candidate rows (txn_date %s .. %s)",
             len(candidate_rows), candidate_rows[0]["txn_date"],
             candidate_rows[-1]["txn_date"])

    # Scrape Gmail once for the window we care about - widened to cover the
    # matcher's 14-day lookback from the oldest candidate txn.
    if args.since:
        scrape_from = args.since
    else:
        scrape_from = min(r["txn_date"] for r in candidate_rows) - timedelta(days=14)
    orders = scrape_orders(settings, scrape_from)
    log.info("total parsed orders available for matching: %d", len(orders))

    matched_with_email = 0
    skipped_no_context = 0
    processed = 0
    t0 = time.monotonic()

    for row in candidate_rows:
        if processed >= args.limit:
            break

        payee = row["payee"] or ""
        up = payee.upper()
        source = "amazon" if "AMAZON" in up or "AMZN" in up else (
                 "venmo" if "VENMO" in up else None)

        email_summary = None
        if source:
            candidates = [o for o in orders if o["source"] == source]
            best = find_best_match(candidates, row, source=source)
            if best:
                email_summary = best["summary"]

        memo = (row["memo"] or "").strip()
        is_generic = bool(_GENERIC_PAYEE_RE.match(payee))

        # Decide context for the DM and whether the row is DM-worthy.
        # Generic payees (Amazon/Venmo/etc.) need an email match or useful
        # memo, or the user can't categorize blind. Specific payees are
        # fine with just payee+amount.
        if email_summary:
            raw_summary = email_summary
            context_kind = "email"
            matched_with_email += 1
        elif _useful_memo(memo, payee):
            raw_summary = memo
            context_kind = "memo"
        elif is_generic and not args.no_skip:
            skipped_no_context += 1
            log.info("skip (generic payee, no context): %s $%.2f %s",
                     row["txn_date"], row["amount_cents"] / 100, payee[:30])
            if not args.dry_run:
                with storage.connect(settings.paths.database) as con:
                    con.execute(
                        "UPDATE pending_txn SET status = 'skipped' "
                        "WHERE id = ?", (row["id"],),
                    )
            continue
        else:
            # Specific payee with no extra context - payee + amount is
            # enough for the user to categorize. raw_summary stays NULL
            # so format_item_prompt falls back to just payee + amount.
            raw_summary = None
            context_kind = "payee"

        # Run the LLM. The summary we feed it mirrors what the user will
        # see in the DM, so they're judging the same thing the model saw.
        summary_for_llm = raw_summary or f"{payee} | {memo}".strip(" |") or payee

        # Phase 3.2: inject historical priors for this payee. The LLM strongly
        # biases toward the categories Steven actually uses for this merchant,
        # which fixes the "Amazon → Chase Amazon" / "Coinbase → Alternate
        # Investment vs. Uncategorized" failure modes that motivated this work.
        priors = storage.get_category_priors_for_payee(
            settings.paths.database, payee, top_n=5,
        )

        ts = time.monotonic()
        result = cat_engine.suggest(
            summary=summary_for_llm,
            amount_cents=row["amount_cents"],
            date_str=str(row["txn_date"]),
            source=source or "ynab",
            categories=categories,
            priors=priors,
        )
        latency_ms = int((time.monotonic() - ts) * 1000)

        cat_id = result.get("category_id")
        cat_name = next((c["name"] for c in categories if c["id"] == cat_id), "(none)")
        conf = float(result.get("confidence", 0) or 0)
        processed += 1

        log.info("[%d/%d] %s $%.2f %s -> %s (conf=%.2f, %dms, %s)",
                 processed, args.limit, row["txn_date"],
                 row["amount_cents"] / 100, payee[:25],
                 cat_name[:25], conf, latency_ms, context_kind)

        if not args.dry_run and cat_id:
            with storage.connect(settings.paths.database) as con:
                con.execute(
                    "UPDATE pending_txn SET suggested_category = ?, "
                    "raw_summary = ? WHERE id = ?",
                    (cat_id, raw_summary, row["id"]),
                )

    elapsed = int(time.monotonic() - t0)
    log.info("=" * 60)
    log.info("DONE: %d rows enriched in %ds  (email: %d, other: %d, skipped no-context: %d)",
             processed, elapsed, matched_with_email,
             processed - matched_with_email, skipped_no_context)
    if args.dry_run:
        log.info("DRY RUN - no DB writes")
    storage.audit(settings.paths.database, "daily_catchup",
                  {"processed": processed,
                   "matched_with_email": matched_with_email,
                   "elapsed_s": elapsed,
                   "dry_run": args.dry_run})


if __name__ == "__main__":
    main()
