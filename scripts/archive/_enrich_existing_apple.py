"""Find existing APPLE.COM/BILL pending_txn / pending_order rows and enrich
them with the matching apple_receipt content from Steven's Gmail.

One-shot retroactive fix. Future Apple charges get this automatically via the
gmail_watcher → ingest_signal pipeline once the bot restarts.
"""
from __future__ import annotations
import os
from datetime import timedelta, date

from bot import storage
from bot.config import load_settings
from bot.gmail_imap import GmailIMAP, extract_body, extract_headers
from bot.parsers.apple_receipt import parse as parse_apple

settings = load_settings()
account = next(a for a in settings.gmail_accounts if a.email == "sbharri2@gmail.com")
app_pw = os.environ.get(getattr(account, "imap_password_env", "STEVEN_GMAIL_IMAP_PASSWORD"), "")

# Pull all Apple receipts of the last 21 days so we can scan amount+date.
with GmailIMAP(account.email, app_pw) as imap:
    uids = imap.search('from:no_reply@email.apple.com subject:"Your receipt from Apple" newer_than:21d')
    receipts = []
    for uid in uids:
        msg = imap.fetch_message(uid)
        if msg is None:
            continue
        body = extract_body(msg)
        headers = extract_headers(msg)
        parsed = parse_apple(body, subject=headers.get("Subject", ""),
                             date_header=headers.get("Date", ""))
        if parsed["parse_status"] in {"ok", "partial"} and parsed["total_cents"]:
            receipts.append(parsed)

print(f"Scraped {len(receipts)} apple receipts")
for r in receipts:
    print(f"  {r['order_date']}  ${r['total_cents']/100:.2f}  {r['summary']}")

# Find pending_txn rows with APPLE-ish payees that are still pending
with storage.connect("ynab_helper.db") as con:
    rows = con.execute(
        """SELECT id, txn_date, payee, amount_cents, raw_summary, suggested_category, status
           FROM pending_txn
           WHERE status = 'pending'
             AND (payee LIKE '%APPLE.COM%' OR payee LIKE '%APL*%' OR payee LIKE '%Apple%')
           ORDER BY txn_date DESC"""
    ).fetchall()
    print(f"\nFound {len(rows)} pending APPLE pending_txn(s):")
    for r in rows:
        print(f"  #{r['id']:>4}  {r['txn_date']}  ${abs(r['amount_cents'])/100:.2f}  "
              f"payee={r['payee']!r}")
        # Find matching receipt by amount + date (±3 days, both directions)
        txn_date = r["txn_date"] if isinstance(r["txn_date"], date) \
            else date.fromisoformat(str(r["txn_date"]))
        amt = abs(r["amount_cents"])
        match = None
        for rcpt in receipts:
            if abs(rcpt["total_cents"] - amt) > 50:  # tolerance: 50 cents
                continue
            if abs((rcpt["order_date"] - txn_date).days) > 3:
                continue
            match = rcpt
            break
        if not match:
            print(f"     no receipt match")
            continue
        new_summary = (
            f"Apple charge: {match['summary'][len('Apple: '):] if match['summary'].startswith('Apple: ') else match['summary']} "
            f"(receipt {match['order_id']})"
        )
        print(f"     MATCH: {match['order_date']} ${match['total_cents']/100:.2f} "
              f"-> summary={new_summary!r}")
        con.execute(
            "UPDATE pending_txn SET raw_summary = ?, "
            "memo = COALESCE(NULLIF(memo, ''), '') || ' [enriched from Apple receipt ' || ? || ']' "
            "WHERE id = ?",
            (new_summary, match["order_id"], r["id"]),
        )

storage.audit(
    "ynab_helper.db",
    "apple_retroactive_enrich",
    {"scraped_receipts": len(receipts), "pending_apple_rows": len(rows)},
)
print("\ndone")
