"""Re-ingest the multi-line Coastal email so the missing $79.88 line
gets its own row now that fan-out skips fuzzy dedupe.
"""
import os
from bot import storage
from bot.config import load_settings
from bot.envelope import recompute_month
from bot.gmail_imap import GmailIMAP, extract_body, extract_headers, message_id_for_dedupe
from bot.parsers.coastal_transaction_alert import parse as parse_coastal
from bot.ingest import ingest_signal

settings = load_settings()
account = next(a for a in settings.gmail_accounts if a.email == "sbharri2@gmail.com")
pw = os.environ.get(getattr(account, "imap_password_env", "STEVEN_GMAIL_IMAP_PASSWORD"), "")

with GmailIMAP(account.email, pw) as imap:
    uids = imap.search('from:coastal24.com subject:"Transaction Alert" newer_than:2d')
    for uid in uids:
        msg = imap.fetch_message(uid)
        if msg is None:
            continue
        body = extract_body(msg)
        if "MASSMUTUAL" not in body.upper():
            continue
        headers = extract_headers(msg)
        eid = message_id_for_dedupe(msg, uid)
        parsed = parse_coastal(body, subject=headers.get("Subject", ""),
                               date_header=headers.get("Date", ""))
        print(f"re-ingest email — primary ${parsed['amount_cents']/100:+.2f}, "
              f"{len(parsed.get('additional_txns') or [])} extras")
        result = ingest_signal(
            "ynab_helper.db",
            signal_kind="coastal_transaction_alert",
            email_id=eid,
            parsed=parsed,
            user_id="steven",
            settings=settings,
        )
        print(f"  result: {result}")

print("\nAll June MASSMUTUAL/MASSACHUSETTS rows now:")
with storage.connect("ynab_helper.db") as con:
    for r in con.execute(
        "SELECT id, posted_date, amount_cents, payee FROM ledger_txn "
        "WHERE posted_date >= '2026-06-01' "
        "  AND (LOWER(payee) LIKE '%mass%') ORDER BY id"
    ).fetchall():
        d = dict(r)
        print(f"  lt#{d['id']}  {d['posted_date']}  ${d['amount_cents']/100:+.2f}  {d['payee']}")

recompute_month("ynab_helper.db", "2026-06")
print("\nrecompute done")
