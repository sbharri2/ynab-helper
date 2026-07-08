"""One-time backfill of Amazon order-confirmation receipts.

The live poller only searches `newer_than:Nd`, so any Amazon order email that
arrived before the bot was watching (or during downtime) was never captured as
a `pending_order`. Those emails are still in Gmail, though — this script sweeps
a fixed date range and inserts each as a receipt so the reconciliation tab and
the matcher have something to pair historical charges against.

Reuses the bot's own IMAP machinery + amazon parser + idempotent insert. It is
NON-INVASIVE: it does not categorize (no LLM), does not relabel/mark emails in
Gmail, and inserts are idempotent on email_id (safe to re-run).

Usage:
    .venv/Scripts/python.exe scripts/backfill_amazon_orders.py [--since 2026/01/01]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Make `bot` importable when run from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader — sets vars that aren't already in the environment."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026/01/01",
                    help="Gmail after: date (YYYY/MM/DD). Default 2026/01/01.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Search + parse but do not insert.")
    args = ap.parse_args()

    repo = Path(__file__).resolve().parents[1]
    # Load creds exactly like the bot does (python-dotenv). override=True so a
    # stale value already in the shell env can't shadow .env.
    from dotenv import load_dotenv
    load_dotenv(repo / ".env", override=True)

    from bot import storage
    from bot.config import load_settings
    from bot.gmail_watcher import _account_imap_password, _iter_imap, _load_parser

    settings = load_settings(repo / "config.yaml")
    db = settings.paths.database

    steven = next((a for a in settings.gmail_accounts if a.user_id == "steven"), None)
    if steven is None:
        print("ERROR: no gmail account with user_id='steven' in config.yaml")
        return 1
    pw = _account_imap_password(steven)
    if not pw:
        print(f"ERROR: IMAP password env {steven.imap_password_env!r} not set "
              f"(checked environment + .env)")
        return 1

    query = f"from:auto-confirm@amazon.com subject:Ordered after:{args.since}"
    print(f"Account : {steven.email}")
    print(f"Query   : {query}")
    print(f"DB      : {db}")
    print()

    with storage.connect(db) as con:
        before = con.execute(
            "SELECT COUNT(*) FROM pending_order WHERE source='amazon'"
        ).fetchone()[0]

    from bot.gmail_imap import (
        GmailIMAP, extract_body as imap_extract_body,
        extract_headers as imap_extract_headers,
        message_id_for_dedupe as imap_msg_id,
    )
    parse = _load_parser("amazon")

    seen = ok = partial = failed = inserted = 0
    msg_iter = _iter_imap(
        steven.email, pw, query,
        GmailIMAP, imap_extract_body, imap_extract_headers, imap_msg_id,
    )
    for email_id, body, headers, _mark_done in msg_iter:
        seen += 1
        parsed = parse(
            body,
            subject=headers.get("Subject", ""),
            date_header=headers.get("Date", ""),
        )
        status = parsed.get("parse_status")
        if status == "ok":
            ok += 1
        elif status == "partial":
            partial += 1
        else:
            failed += 1
            continue  # mirror the poller: never queue a non-order

        if args.dry_run:
            continue
        try:
            storage.insert_pending_order(
                db,
                user_id=steven.user_id,
                source=parsed["source"],
                external_id=parsed.get("order_id"),
                email_id=email_id,
                order_date=parsed.get("order_date"),
                total_cents=parsed.get("total_cents") or 0,
                raw_summary=parsed.get("summary", ""),
                raw_payload=parsed,
            )
            inserted += 1
        except Exception as e:  # noqa: BLE001
            print(f"  insert failed for {email_id}: {e}")

    with storage.connect(db) as con:
        after = con.execute(
            "SELECT COUNT(*) FROM pending_order WHERE source='amazon'"
        ).fetchone()[0]

    print(f"Emails matched  : {seen}")
    print(f"  parsed ok     : {ok}")
    print(f"  parsed partial: {partial}")
    print(f"  parse failed  : {failed}  (skipped)")
    if not args.dry_run:
        print(f"Insert attempts : {inserted}")
        print(f"pending_order(amazon): {before} -> {after}  (+{after - before} new)")
    else:
        print("(dry-run: nothing inserted)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
