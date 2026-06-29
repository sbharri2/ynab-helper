"""Did the bot's gmail_watcher actually poll apple_receipt?"""
import os
from datetime import date, timedelta
from bot.config import load_settings
from bot.gmail_imap import GmailIMAP

settings = load_settings()

# Find the apple_receipt source
apple_src = next((s for s in settings.email_sources if s.name == "apple_receipt"), None)
print(f"apple_receipt source: {apple_src}")
print(f"  query: {apple_src.query!r}")

# Look at last 3 days only since the query is newer_than:3d
account = next(a for a in settings.gmail_accounts if a.email == "sbharri2@gmail.com")
pw = os.environ.get(getattr(account, "imap_password_env", "STEVEN_GMAIL_IMAP_PASSWORD"), "")

with GmailIMAP(account.email, pw) as imap:
    uids = imap.search(apple_src.query)
    print(f"\nApple receipts matching '{apple_src.query}': {len(uids)} hit(s)")
    for uid in uids[-3:]:
        msg = imap.fetch_message(uid)
        if msg is not None:
            from bot.gmail_imap import extract_headers
            h = extract_headers(msg)
            print(f"  uid {uid.decode()}: {h.get('Subject','')[:60]}  {h.get('Date','')[:40]}")
