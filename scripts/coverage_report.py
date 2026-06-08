"""Amazon coverage report — am I catching every Amazon charge?

Pulls all Amazon-related charges from the last N days across every
signal source, cross-references against ``pending_order`` rows (the
email side) and the historical ``orders.json`` dump.

For each charge it answers two questions:
  1. Do we have a CC alert / YNAB sync confirming the charge hit
     a bank account? (the capture question)
  2. Do we have item-level context from a matching email or the
     pasted Amazon order history? (the enrichment question)

Output groups:
  ✓ FULL      — charge captured AND item context available
  ⚠ NO ITEMS  — charge captured but no email/order data (you'd
                 categorize blind without YNAB memo / Amazon login)
  ⚠ NO CHARGE — email confirmation exists but no matching CC/YNAB
                 row (parser issue, last4 wrong, or order canceled)

Run any time:
    python -m scripts.coverage_report                  # default 30d
    python -m scripts.coverage_report --days 7
    python -m scripts.coverage_report --days 90 --verbose
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import date, timedelta
from pathlib import Path

from bot import storage
from bot.config import load_settings

log = logging.getLogger("coverage")

AMAZON_PAYEE_PATTERNS = (
    "%AMAZON%",
    "%AMZN%",
)
ORDERS_DUMP = Path("_LOCAL_SECRETS_/amazon_history/orders.json")
# Match window — bank CC posting lags the order by 0-5 days typically.
MATCH_WINDOW_DAYS = 5


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=30,
                   help="Lookback window. Default 30.")
    p.add_argument("--verbose", action="store_true",
                   help="Print every matched row, not just summary.")
    return p.parse_args()


def _load_dump_orders() -> list[dict]:
    if not ORDERS_DUMP.exists():
        return []
    data = json.loads(ORDERS_DUMP.read_text(encoding="utf-8"))
    out = []
    for o in data.get("orders", []):
        try:
            out.append({
                "order_date": date.fromisoformat(o["date"]),
                "order_id": o["order_id"],
                "total_cents": int(o["total_cents"]),
                "items": o["items"],
                "merchant": o.get("merchant", "Amazon"),
            })
        except (KeyError, ValueError):
            continue
    return out


def _amount_match(a: int, b: int, tolerance_cents: int = 100) -> bool:
    """Allow $1 tolerance to absorb tax-vs-pre-tax mismatches."""
    return abs(abs(int(a)) - abs(int(b))) <= tolerance_cents


def _date_in_window(charge_d: date, order_d: date, window: int) -> bool:
    """Charge typically posts 0-window days AFTER the order."""
    delta = (charge_d - order_d).days
    return 0 <= delta <= window


def main():
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = load_settings()
    db = settings.paths.database
    cutoff = date.today() - timedelta(days=args.days)

    # ---- 1. Pull every Amazon-related CHARGE we have a record of ----
    where_clauses = " OR ".join(["UPPER(payee) LIKE ?"] * len(AMAZON_PAYEE_PATTERNS))
    sql_args = list(AMAZON_PAYEE_PATTERNS) + [cutoff]
    with storage.connect(db) as con:
        charge_rows = con.execute(
            f"""SELECT id, posted_date, amount_cents, payee, source_signal,
                       memo, category_id
                FROM ledger_txn
                WHERE ({where_clauses})
                  AND posted_date >= ?
                ORDER BY posted_date DESC""",
            sql_args,
        ).fetchall()
        # pending_order Amazon rows — what we have from email parsing
        email_rows = con.execute(
            """SELECT id, order_date, total_cents, raw_summary,
                      external_id, status
               FROM pending_order
               WHERE source = 'amazon'
                 AND order_date >= ?
               ORDER BY order_date DESC""",
            (cutoff,),
        ).fetchall()

    charges = [dict(r) for r in charge_rows]
    emails = [dict(r) for r in email_rows]
    dump_orders = [o for o in _load_dump_orders() if o["order_date"] >= cutoff]

    # ---- 2. For each charge, find a matching email or dump entry ----
    full_match: list[tuple] = []
    no_items: list[dict] = []

    for c in charges:
        cdate = c["posted_date"]
        if isinstance(cdate, str):
            cdate = date.fromisoformat(cdate)
        amt = c["amount_cents"]
        # Try email first
        email = next(
            (e for e in emails
             if _amount_match(amt, e["total_cents"])
                and _date_in_window(cdate, e["order_date"], MATCH_WINDOW_DAYS)),
            None,
        )
        if email:
            full_match.append((c, "email", email))
            continue
        # Fall back to historical dump
        dump_hit = next(
            (o for o in dump_orders
             if _amount_match(amt, o["total_cents"])
                and _date_in_window(cdate, o["order_date"], MATCH_WINDOW_DAYS)),
            None,
        )
        if dump_hit:
            full_match.append((c, "dump", dump_hit))
            continue
        no_items.append(c)

    # ---- 3. Find emails / dump entries with no matching charge ----
    matched_email_ids = {e["id"] for _, src, e in full_match if src == "email"}
    matched_dump_ids = {o["order_id"] for _, src, o in full_match if src == "dump"}
    orphan_emails = [e for e in emails if e["id"] not in matched_email_ids]
    orphan_dumps = [o for o in dump_orders
                    if o["order_id"] not in matched_dump_ids]

    # ---- 4. Summarize ----
    print()
    print(f"AMAZON COVERAGE — last {args.days} days")
    print("=" * 60)
    print(f"  Total CC/YNAB charges seen:        {len(charges):>4}")
    print(f"    ✓ full context (email or dump):  {len(full_match):>4}")
    print(f"    ⚠ no items context:              {len(no_items):>4}")
    print()
    print(f"  Total email confirmations:         {len(emails):>4}")
    print(f"    ⚠ no matching charge:            {len(orphan_emails):>4}")
    print()
    if dump_orders:
        print(f"  Dump orders in window:             {len(dump_orders):>4}")
        print(f"    ⚠ no matching charge:            {len(orphan_dumps):>4}")
        print()

    if no_items:
        print("⚠ CHARGES WITH NO ITEM CONTEXT (categorize blind):")
        for c in no_items:
            cat_id = c["category_id"]
            with storage.connect(db) as con:
                cat = con.execute(
                    "SELECT name FROM category WHERE id=?", (cat_id,),
                ).fetchone() if cat_id else None
            print(f"    {c['posted_date']}  ${c['amount_cents']/100:>8.2f}  "
                  f"{(c['payee'] or '')[:30]:30s}  "
                  f"cat={cat['name'] if cat else '(none)'}")
        print()

    if orphan_emails:
        print("⚠ EMAIL ORDERS WITH NO MATCHING CHARGE:")
        for e in orphan_emails:
            print(f"    {e['order_date']}  ${e['total_cents']/100:>8.2f}  "
                  f"{(e['raw_summary'] or '')[:50]:50s}  "
                  f"status={e['status']}")
        print()

    if args.verbose and full_match:
        print("✓ FULL-CONTEXT CHARGES:")
        for c, src, m in full_match:
            kind_label = "email" if src == "email" else "dump"
            if src == "email":
                ctx = (m["raw_summary"] or "")[:45]
            else:
                ctx = m["items"][:45]
            print(f"    {c['posted_date']}  ${c['amount_cents']/100:>8.2f}  "
                  f"[{kind_label}] {ctx}")

    # ---- 5. Verdict ----
    coverage_pct = (len(full_match) / len(charges) * 100) if charges else 0
    capture_pct = 100.0 if not orphan_emails else (
        (len(emails) - len(orphan_emails)) / len(emails) * 100
    )
    print("VERDICT")
    print("-" * 60)
    if not charges:
        print(f"  No Amazon charges in last {args.days} days.")
    else:
        print(f"  Context coverage:  {coverage_pct:5.1f}%  "
              f"({len(full_match)} / {len(charges)} charges have item details)")
    if emails:
        print(f"  Capture rate:      {capture_pct:5.1f}%  "
              f"({len(emails) - len(orphan_emails)} / {len(emails)} email orders matched to a charge)")
    if not no_items and not orphan_emails:
        print()
        print("  ✓ ALL ACCOUNTED FOR. Every Amazon charge has email context, "
              "every email order has a matching charge.")
    print()


if __name__ == "__main__":
    main()
