"""Scout Apple receipt emails in Steven's Gmail (IMAP).

Goal: see what Apple's receipt emails look like so we can write a parser.
Pulls subject + first ~600 chars of body for the most recent few hits.
Targets Apple's known sender addresses and the $5.99 charge from 2026-06-07.
"""
from __future__ import annotations
import os
from bot.config import load_settings
from bot.gmail_imap import GmailIMAP, extract_body, extract_headers

settings = load_settings()
account = next(a for a in settings.gmail_accounts if a.email == "sbharri2@gmail.com")
pw_env = getattr(account, "imap_password_env", "STEVEN_GMAIL_IMAP_PASSWORD")
app_pw = os.environ.get(pw_env or "STEVEN_GMAIL_IMAP_PASSWORD", "")
if not app_pw:
    print(f"NO PASSWORD in env var {pw_env}")
    raise SystemExit(1)

QUERIES = [
    'from:do_not_reply@apple.com newer_than:21d',
    'from:no_reply@email.apple.com newer_than:21d',
    'from:apple.com subject:receipt newer_than:21d',
    'from:apple.com subject:invoice newer_than:21d',
    '(from:apple.com OR from:itunes.com) newer_than:21d',
]

with GmailIMAP(account.email, app_pw) as imap:
    for q in QUERIES:
        uids = imap.search(q)
        print(f"\n=== query: {q} -> {len(uids)} hit(s) ===")
        # Pull the 4 most recent
        for uid in uids[-4:]:
            msg = imap.fetch_message(uid)
            if msg is None:
                continue
            h = extract_headers(msg)
            body = (extract_body(msg) or "")
            # Strip excess whitespace for display
            body = " ".join(body.split())
            print(f"  uid={uid.decode()}")
            print(f"    From:    {h.get('From', '')[:80]}")
            print(f"    Date:    {h.get('Date', '')[:60]}")
            print(f"    Subject: {h.get('Subject', '')[:90]}")
            print(f"    Body:    {body[:500]}")
            print()
