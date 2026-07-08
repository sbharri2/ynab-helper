"""Repair Amazon receipts whose total_cents is 0 (stale parse).

A handful of pending_order rows were captured by an older parser version that
failed to read the "Grand Total: $X.XX" line, so they sit at total_cents=0 and
can never match their charge. The current parser handles those emails fine —
this re-fetches each stale order's email (Steven's mailbox, then Allison's) and
re-parses it, updating total/summary in place when a real total is recovered.

Usage: .venv/Scripts/python.exe scripts/reparse_zero_total_orders.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from dotenv import load_dotenv
    repo = Path(__file__).resolve().parents[1]
    load_dotenv(repo / ".env", override=True)

    from bot import storage
    from bot.config import load_settings
    from bot.gmail_imap import GmailIMAP, extract_body, extract_headers
    from bot.gmail_watcher import _account_imap_password
    from bot.parsers import amazon

    settings = load_settings(repo / "config.yaml")
    db = settings.paths.database

    # (email, app_password) for every account with IMAP configured.
    mailboxes = []
    for acct in settings.gmail_accounts:
        pw = _account_imap_password(acct)
        if pw:
            mailboxes.append((acct.email, pw))

    with storage.connect(db) as con:
        stale = [dict(r) for r in con.execute(
            "SELECT id, external_id, email_id FROM pending_order "
            "WHERE source='amazon' AND COALESCE(total_cents,0) <= 0"
        )]
    print(f"Stale (total_cents<=0) Amazon receipts: {len(stale)}")

    fixed = 0
    for row in stale:
        onum = row["external_id"]
        # Fetch the EXACT original email by its stored Message-ID — an
        # order-number full-text search can return the wrong email (a shipment
        # notice, or another order that mentions the number).
        got = None
        for email, pw in mailboxes:
            try:
                with GmailIMAP(email, pw) as imap:
                    uids = imap.search(f"rfc822msgid:{row['email_id']}")
                    for uid in uids:
                        msg = imap.fetch_message(uid)
                        if not msg:
                            continue
                        h = extract_headers(msg)
                        parsed = amazon.parse(
                            extract_body(msg),
                            subject=h.get("Subject", ""),
                            date_header=h.get("Date", ""),
                        )
                        if parsed.get("total_cents"):
                            got = parsed
                            break
            except Exception as e:  # noqa: BLE001
                print(f"  [{email}] msgid fetch failed for {onum}: {e}")
            if got:
                break

        if not got:
            print(f"  rcpt#{row['id']} {onum}: no total recovered")
            continue

        newtot = got["total_cents"]
        print(f"  rcpt#{row['id']} {onum}: total 0 -> ${newtot/100:.2f}  "
              f"({got.get('summary','')[:45]})")
        fixed += 1
        if not args.dry_run:
            with storage.connect(db) as con:
                con.execute(
                    "UPDATE pending_order SET total_cents=?, raw_summary=?, "
                    "raw_payload=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (newtot, got.get("summary", ""),
                     json.dumps(got, default=str), row["id"]),
                )

    print(f"\n{'Would fix' if args.dry_run else 'Fixed'} {fixed} receipt(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
