"""Pull the actual body of the Coastal alert that misparsed MassMutual."""
import os
from bot.config import load_settings
from bot.gmail_imap import GmailIMAP, extract_body, extract_headers

settings = load_settings()
account = next(a for a in settings.gmail_accounts if a.email == "sbharri2@gmail.com")
pw = os.environ.get(getattr(account, "imap_password_env", "STEVEN_GMAIL_IMAP_PASSWORD"), "")

with GmailIMAP(account.email, pw) as imap:
    uids = imap.search('from:coastal24.com subject:"Transaction Alert" newer_than:2d')
    print(f"Found {len(uids)} coastal txn alerts in last 2 days")
    for uid in uids:
        msg = imap.fetch_message(uid)
        if msg is None:
            continue
        body = extract_body(msg)
        headers = extract_headers(msg)
        if "MASSMUTUAL" not in body.upper():
            continue
        print(f"\n--- uid {uid.decode()} ---")
        print(f"Subject: {headers.get('Subject', '')}")
        print(f"Date:    {headers.get('Date', '')}")
        print("Body:")
        for i, ln in enumerate(body.splitlines()[:25], 1):
            print(f"  {i:2d}| {ln!r}")
