"""One-shot: parse Allison's Amazon/Venmo emails (saved as MCP get_thread
JSON files) and use them to enrich pending_txn rows the LLM previously had
no context for. Re-runs the LLM on enriched rows.

Inputs: _LOCAL_SECRETS_/allison_emails/*.txt  (raw MCP get_thread output)
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import time
from datetime import date
from email.utils import parsedate_to_datetime
from pathlib import Path

from bot import storage
from bot.categorizer import Categorizer
from bot.config import load_settings
from bot.matcher import find_best_match
from bot.parsers import amazon as amazon_parser
from bot.parsers import amazon_shipment as amazon_shipment_parser
from bot.parsers import venmo as venmo_parser
from bot.ynab_client import YnabClient

log = logging.getLogger("allison_backfill")

EMAILS_DIR = Path("_LOCAL_SECRETS_/allison_emails")


SENDER_TO_PARSER = {
    "shipment-tracking@amazon.com": ("amazon_shipment", amazon_shipment_parser.parse),
    "auto-confirm@amazon.com": ("amazon", amazon_parser.parse),
    "venmo@venmo.com": ("venmo", venmo_parser.parse),
}


def load_email_files() -> list[dict]:
    """Reads all saved MCP get_thread JSON files; returns list of message dicts."""
    out = []
    for f in EMAILS_DIR.glob("*.txt"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("skip %s: %s", f.name, e)
            continue
        for msg in data.get("messages", []):
            sender = msg.get("sender", "").lower()
            if sender in SENDER_TO_PARSER:
                out.append(msg)
    return out


def parse_messages_to_orders(messages: list[dict]) -> list[dict]:
    orders = []
    for msg in messages:
        sender = msg["sender"].lower()
        parser_name, parser_fn = SENDER_TO_PARSER[sender]
        body = msg.get("plaintextBody", "") or ""
        subject = msg.get("subject", "") or ""
        # Convert ISO date back to RFC2822-ish string the parser can handle.
        # MCP returns ISO; the parser uses parsedate_to_datetime which is
        # forgiving but works best on the email-Date format. Easier: pass
        # the ISO string and let it fall back to today() in the parser.
        date_header = msg.get("date", "") or ""
        try:
            parsed = parser_fn(
                body, subject=subject, date_header=date_header,
            )
        except Exception as e:
            log.warning("parse failed (%s): %s", parser_name, e)
            continue
        if parsed.get("parse_status") not in {"ok", "partial"}:
            continue
        # Try to pull a real order_date out of the ISO timestamp if the
        # parser fell back to today (which would break date-proximity matching).
        try:
            actual_date = parsedate_to_datetime(date_header).date() \
                if "T" not in date_header \
                else date.fromisoformat(date_header.split("T")[0])
        except Exception:
            actual_date = parsed.get("order_date") or date.today()

        orders.append({
            "source": parsed.get("source", parser_name.replace("_shipment", "")),
            "parser": parser_name,
            "external_id": parsed.get("order_id") or parsed.get("counterparty") or "",
            "order_date": actual_date,
            "total_cents": parsed.get("total_cents")
                           or parsed.get("amount_cents") or 0,
            "summary": parsed.get("summary", ""),
        })
    return orders


def _dedupe_amazon_orders(orders: list[dict]) -> list[dict]:
    """Same dedup as daily_catchup: drop auto-confirm rows when a
    shipment-tracking row exists for the same order_id."""
    shipped_ids = {
        o["external_id"]
        for o in orders
        if o.get("parser") == "amazon_shipment" and o.get("external_id")
    }
    return [
        o for o in orders
        if not (o.get("parser") == "amazon" and o.get("external_id") in shipped_ids)
    ]


def pick_amazon_venmo_rows(db_path: str) -> list[dict]:
    """Pending pending_txn rows whose payee looks Amazon/Venmo. Includes
    already-suggested rows so we can overwrite bad suggestions with
    email-enriched ones."""
    with storage.connect(db_path) as con:
        rows = con.execute(
            """SELECT * FROM pending_txn
               WHERE status = 'pending'
                 AND (payee LIKE '%Amazon%' OR payee LIKE '%AMZN%'
                      OR payee LIKE '%Venmo%' OR payee LIKE '%VENMO%')
               ORDER BY txn_date ASC""",
        ).fetchall()
        return [dict(r) for r in rows]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    settings = load_settings()

    msgs = load_email_files()
    log.info("loaded %d email messages from disk", len(msgs))

    raw_orders = parse_messages_to_orders(msgs)
    log.info("parsed %d orders before dedup", len(raw_orders))

    orders = _dedupe_amazon_orders(raw_orders)
    log.info("orders after dedup: %d", len(orders))
    by_source: dict[str, int] = {}
    for o in orders:
        by_source[o["source"]] = by_source.get(o["source"], 0) + 1
    log.info("  by source: %s", by_source)

    rows = pick_amazon_venmo_rows(settings.paths.database)
    log.info("pending_txn rows to attempt matching: %d", len(rows))

    cat_engine = Categorizer(
        settings.ollama.endpoint, settings.ollama.model, settings.ollama.temperature,
    )
    ynab = YnabClient(settings.ynab_token, settings.ynab.budget_id)
    log.info("fetching YNAB categories ...")
    categories = ynab.list_categories()
    log.info("  %d categories", len(categories))

    matched = 0
    relabeled = 0

    for row in rows:
        payee = row["payee"] or ""
        up = payee.upper()
        source = "amazon" if "AMAZON" in up or "AMZN" in up else "venmo"
        candidates = [o for o in orders if o["source"] == source]
        best = find_best_match(candidates, row, source=source)
        if not best:
            continue
        matched += 1
        new_summary = best["summary"]
        log.info(
            "[match] #%d %s $%.2f %s  ->  %s",
            row["id"], row["txn_date"], row["amount_cents"] / 100,
            payee[:25], new_summary[:80],
        )

        # Re-run LLM with new context
        ts = time.monotonic()
        result = cat_engine.suggest(
            summary=new_summary,
            amount_cents=row["amount_cents"],
            date_str=str(row["txn_date"]),
            source=source,
            categories=categories,
        )
        latency_ms = int((time.monotonic() - ts) * 1000)
        cat_id = result.get("category_id")
        cat_name = next((c["name"] for c in categories if c["id"] == cat_id), "(none)")
        conf = float(result.get("confidence", 0) or 0)
        log.info(
            "  -> %s (conf=%.2f, %dms)", cat_name[:30], conf, latency_ms,
        )

        if not args.dry_run and cat_id:
            with storage.connect(settings.paths.database) as con:
                con.execute(
                    """UPDATE pending_txn
                       SET suggested_category = ?, raw_summary = ?
                       WHERE id = ?""",
                    (cat_id, new_summary, row["id"]),
                )
            relabeled += 1

    log.info("=" * 60)
    log.info("DONE: matched %d, relabeled %d", matched, relabeled)
    if args.dry_run:
        log.info("DRY RUN - no DB writes")


if __name__ == "__main__":
    main()
