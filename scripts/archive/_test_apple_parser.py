"""Quick sanity check: feed each recent Apple receipt through the new
parser and print what it extracts.
"""
from __future__ import annotations
import os
from bot.config import load_settings
from bot.gmail_imap import GmailIMAP, extract_body, extract_headers
from bot.parsers.apple_receipt import parse

settings = load_settings()
account = next(a for a in settings.gmail_accounts if a.email == "sbharri2@gmail.com")
pw_env = getattr(account, "imap_password_env", "STEVEN_GMAIL_IMAP_PASSWORD")
app_pw = os.environ.get(pw_env or "STEVEN_GMAIL_IMAP_PASSWORD", "")

with GmailIMAP(account.email, app_pw) as imap:
    uids = imap.search('from:no_reply@email.apple.com subject:"Your receipt from Apple" newer_than:21d')
    print(f"Testing {len(uids)} Apple receipts:\n")
    for uid in uids:
        msg = imap.fetch_message(uid)
        if msg is None:
            continue
        body = extract_body(msg)
        headers = extract_headers(msg)
        parsed = parse(
            body, subject=headers.get("Subject", ""), date_header=headers.get("Date", ""),
        )
        print(f"--- uid {uid.decode()} | subj: {headers.get('Subject','')[:50]} ---")
        print(f"  status:       {parsed['parse_status']}")
        print(f"  order_id:     {parsed['order_id']}")
        print(f"  order_date:   {parsed['order_date']}")
        print(f"  total_cents:  {parsed['total_cents']} (${(parsed['total_cents'] or 0)/100:.2f})")
        print(f"  card_last4:   {parsed['card_last4']}")
        print(f"  apple_acct:   {parsed['apple_account']}")
        print(f"  items:        {parsed['items']}")
        print(f"  summary:      {parsed['summary']}")
        if parsed["missing_fields"]:
            print(f"  missing:      {parsed['missing_fields']}")
        print()
