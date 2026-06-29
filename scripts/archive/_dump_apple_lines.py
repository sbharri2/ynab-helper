"""Print the exact line-by-line body of one Apple receipt so we can fix item extraction."""
import os
from bot.config import load_settings
from bot.gmail_imap import GmailIMAP, extract_body, extract_headers

settings = load_settings()
account = next(a for a in settings.gmail_accounts if a.email == "sbharri2@gmail.com")
app_pw = os.environ.get(getattr(account, "imap_password_env", "STEVEN_GMAIL_IMAP_PASSWORD"), "")

# Pick three: Apple One ($27.36), V1+VPN ($67.55), NYT Wordle ($5.99)
TARGETS = ["253361", "253650"]

with GmailIMAP(account.email, app_pw) as imap:
    uids = imap.search('from:no_reply@email.apple.com subject:"Your receipt from Apple" newer_than:21d')
    by_str = {u.decode(): u for u in uids}
    for t in TARGETS:
        uid = by_str.get(t)
        if not uid:
            continue
        msg = imap.fetch_message(uid)
        body = extract_body(msg)
        print(f"\n{'='*70}\nUID {t}\n{'='*70}")
        for i, ln in enumerate(body.splitlines(), 1):
            print(f"  {i:3d}| {ln!r}")
