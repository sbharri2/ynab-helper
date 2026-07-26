"""Backfill Amazon orders dropped by the old single-order-per-email parser.

Amazon groups one checkout into several order numbers and sends ONE
"Ordered:" email with a block per order. Until 2026-07-26 the parser read
only the first order id + first Grand Total and discarded the rest, so
those charges hit the card with no receipt to match. Measured on one week
of real mail: 3 of 8 confirmations were multi-order and $336.58 of orders
were dropped — more than the $320.53 being captured.

This re-fetches the confirmation emails over a date window, re-parses them
with the fixed parser, and inserts any `additional_orders` that aren't in
the DB yet. It mirrors bot/gmail_watcher.py exactly:

  * email_id is synthesized as "<parent_email_id>#<order_id>" — the parent
    id alone would collide, because insert_pending_order is idempotent on
    email_id and would collapse every extra back into the first order;
  * assigned_to_user_id comes from _person_from_recipient, same as the
    watcher, so an extra is attributed exactly like its primary;
  * retro_bucket_amazon_order runs per inserted order, so an already-filed
    charge gets re-bucketed to the right person. Charges at/above the
    large-charge threshold are skipped by that function BY DESIGN (spec
    2026-07-25) — they stay in the Inbox for a human decision.

Idempotent: an order whose external_id already exists is skipped.

Envelope caches for any category a retro-bucket touched are refreshed with
envelope.apply_activity_delta — the additive, anchor-preserving path.
NEVER recompute_month, which rebuilds `available` from the identity.

Usage:
    .venv/Scripts/python.exe scripts/backfill_multi_order_amazon.py --dry-run
    .venv/Scripts/python.exe scripts/backfill_multi_order_amazon.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--after", default="2026/07/01",
                    help="Gmail-syntax date, inclusive lower bound")
    ap.add_argument("--before", default="2026/07/09",
                    help="Gmail-syntax date, exclusive upper bound")
    ap.add_argument("--expect", type=int, default=4,
                    help="abort unless exactly this many orders would be "
                         "inserted (blast-radius guard)")
    ap.add_argument("--allow-unexpected-count", action="store_true",
                    help="proceed even when the count differs from --expect")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from dotenv import load_dotenv
    repo = Path(__file__).resolve().parents[1]
    load_dotenv(repo / ".env", override=True)

    from bot import envelope, ingest, storage
    from bot.config import load_settings
    from bot.gmail_imap import GmailIMAP, extract_body, extract_headers
    from bot.gmail_watcher import _account_imap_password, _person_from_recipient
    from bot.parsers import amazon

    settings = load_settings(repo / "config.yaml")
    db = settings.paths.database

    with storage.connect(db) as con:
        known = {r["external_id"] for r in con.execute(
            "SELECT external_id FROM pending_order WHERE source = 'amazon' "
            "AND external_id IS NOT NULL")}

    query = (f"from:auto-confirm@amazon.com after:{args.after} "
             f"before:{args.before} in:anywhere")

    # --- collect candidates ------------------------------------------------
    candidates: list[dict] = []
    for acct in settings.gmail_accounts:
        try:
            password = _account_imap_password(acct)
        except Exception as e:  # noqa: BLE001
            print(f"  ! {acct.email}: no IMAP password ({e}) — skipping")
            continue
        with GmailIMAP(acct.email, password) as im:
            for uid in im.search(query):
                msg = im.fetch_message(uid)
                if msg is None:
                    continue
                headers = extract_headers(msg)
                parsed = amazon.parse(
                    extract_body(msg) or "",
                    subject=headers.get("subject", "") or headers.get("Subject", ""),
                    date_header=headers.get("date", "") or headers.get("Date", ""),
                )
                if parsed["parse_status"] not in {"ok", "partial"}:
                    continue
                parent_id = headers.get("message-id") or headers.get("Message-ID") \
                    or f"uid-{uid.decode() if isinstance(uid, bytes) else uid}"
                for extra in parsed.get("additional_orders") or []:
                    oid = extra.get("order_id")
                    if not oid or oid in known:
                        continue
                    candidates.append({
                        "user_id": acct.user_id,
                        "order_id": oid,
                        "total_cents": extra.get("total_cents") or 0,
                        "summary": extra.get("summary", ""),
                        "payload": extra,
                        "order_date": parsed.get("order_date"),
                        "email_id": f"{parent_id}#{oid}",
                        "person": _person_from_recipient(headers, acct.user_id),
                    })
                    known.add(oid)

    if not candidates:
        print("nothing to backfill.")
        return 0

    for c in candidates:
        print(f"  {c['order_id']}  ${c['total_cents'] / 100:>9,.2f}  "
              f"{c['order_date']}  -> {c['person']}  {c['summary'][:52]}")

    if len(candidates) != args.expect and not args.allow_unexpected_count:
        print(f"\nABORT: matched {len(candidates)} order(s), expected "
              f"{args.expect}. No writes performed. Re-run with "
              f"--expect {len(candidates)} or --allow-unexpected-count "
              f"once the set above looks right.", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"\n[dry-run] would insert {len(candidates)} order(s).")
        return 0

    # --- write -------------------------------------------------------------
    touched: set[tuple[str, str]] = set()   # (month, category_id)
    inserted = 0
    for c in candidates:
        row_id = storage.insert_pending_order(
            db,
            user_id=c["user_id"],
            source="amazon",
            external_id=c["order_id"],
            email_id=c["email_id"],
            order_date=c["order_date"],
            total_cents=c["total_cents"],
            raw_summary=c["summary"],
            raw_payload=c["payload"],
            assigned_to_user_id=c["person"],
        )
        if not row_id:
            print(f"  = {c['order_id']} already present, skipped")
            continue
        inserted += 1

        # Snapshot categories before/after so we know what to refresh.
        with storage.connect(db) as con:
            before = {(r["id"], r["posted_date"][:7], r["category_id"])
                      for r in con.execute(
                          "SELECT id, CAST(posted_date AS TEXT) AS posted_date, "
                          "       category_id FROM ledger_txn "
                          "WHERE ABS(amount_cents) = ? AND is_split = 0",
                          (abs(c["total_cents"]),))}
        try:
            moved = ingest.retro_bucket_amazon_order(db, order_id=row_id)
        except Exception as e:  # noqa: BLE001
            print(f"  ! retro-bucket failed for {c['order_id']}: {e}")
            moved = False
        if moved:
            with storage.connect(db) as con:
                after = {(r["id"], r["posted_date"][:7], r["category_id"])
                         for r in con.execute(
                             "SELECT id, CAST(posted_date AS TEXT) AS posted_date, "
                             "       category_id FROM ledger_txn "
                             "WHERE ABS(amount_cents) = ? AND is_split = 0",
                             (abs(c["total_cents"]),))}
            for _id, month, cat in before ^ after:
                if cat:
                    touched.add((month, cat))
        print(f"  + {c['order_id']}  ${c['total_cents'] / 100:,.2f}  "
              f"po#{row_id}  retro_bucketed={moved}")

    storage.audit(db, "multi_order_amazon_backfill", {
        "inserted": inserted, "window": f"{args.after}..{args.before}",
        "order_ids": [c["order_id"] for c in candidates],
    })

    by_month: dict[str, list[str]] = {}
    for month, cat in touched:
        by_month.setdefault(month, []).append(cat)
    for month, cats in sorted(by_month.items()):
        envelope.apply_activity_delta(db, month, cats)
        print(f"  refreshed {month}: {len(cats)} categor(ies)")

    print(f"\ninserted {inserted} order(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
