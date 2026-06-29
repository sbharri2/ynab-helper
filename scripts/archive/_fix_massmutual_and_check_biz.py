"""(1) Flip lt#23655 from +$110.01 to -$110.01.
(2) Backfill the 3 dropped lines from the same Coastal email
    by re-running the now-fixed parser through ingest_signal.
(3) Print Business Checking reconciliation detail.
"""
from __future__ import annotations
import os
from datetime import date

from bot import storage
from bot.config import load_settings
from bot.envelope import recompute_month
from bot.gmail_imap import GmailIMAP, extract_body, extract_headers
from bot.gmail_imap import message_id_for_dedupe
from bot.parsers.coastal_transaction_alert import parse as parse_coastal
from bot.ingest import ingest_signal

settings = load_settings()

# --- (1) flip the sign on the bad row -----------------------------------
with storage.connect("ynab_helper.db") as con:
    r = con.execute("SELECT amount_cents FROM ledger_txn WHERE id = 23655").fetchone()
    print(f"lt#23655 before: ${r['amount_cents']/100:+.2f}")
    con.execute(
        "UPDATE ledger_txn SET amount_cents = -ABS(amount_cents), "
        "updated_at = CURRENT_TIMESTAMP WHERE id = 23655"
    )
    r = con.execute("SELECT amount_cents FROM ledger_txn WHERE id = 23655").fetchone()
    print(f"lt#23655 after:  ${r['amount_cents']/100:+.2f}")
storage.audit("ynab_helper.db", "massmutual_sign_fix", {"ledger_txn_id": 23655})

# --- (2) re-ingest the email so the 3 dropped lines also get rows -------
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
        print(f"\nRe-ingest email {eid[:40]}...")
        print(f"  primary: ${parsed['amount_cents']/100:+.2f} {parsed['type_word']}")
        print(f"  additional: {len(parsed.get('additional_txns') or [])}")
        result = ingest_signal(
            "ynab_helper.db",
            signal_kind="coastal_transaction_alert",
            email_id=eid,
            parsed=parsed,
            user_id="steven",
            settings=settings,
        )
        print(f"  -> primary action={result['action']}, ledger_txn={result['ledger_txn_id']}")

# --- (3) Business Checking diagnosis ------------------------------------
print("\n" + "="*70)
print("Business Checking reconciliation detail")
print("="*70)
with storage.connect("ynab_helper.db") as con:
    bizrows = con.execute(
        "SELECT id, name, balance_cents FROM account "
        "WHERE LOWER(name) LIKE '%business%checking%' OR LOWER(name) LIKE '%biz%check%'"
    ).fetchall()
    for a in bizrows:
        print(f"\naccount: {a['name']} id={a['id'][:8]}")
        print(f"  ledger.balance_cents (YNAB-imported): ${(a['balance_cents'] or 0)/100:.2f}")
        # Sum ledger_txn for this account
        s = con.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS total, COUNT(*) AS n "
            "FROM ledger_txn WHERE account_id = ?", (a["id"],),
        ).fetchone()
        print(f"  ledger_txn sum: ${s['total']/100:,.2f} ({s['n']} rows)")
        # Latest observed
        obs = con.execute(
            "SELECT as_of_date, balance_cents FROM account_balance_observed "
            "WHERE account_id = ? ORDER BY as_of_date DESC LIMIT 3", (a["id"],),
        ).fetchall()
        print(f"  recent observed:")
        for o in obs:
            print(f"    {o['as_of_date']}  ${o['balance_cents']/100:,.2f}")
        # Recent reconcile_mismatch audit
        ar = con.execute(
            "SELECT id, details, ts FROM audit_log "
            "WHERE event = 'reconcile_mismatch' AND details LIKE ? "
            "ORDER BY id DESC LIMIT 3", (f"%{a['id']}%",),
        ).fetchall()
        for a2 in ar:
            print(f"    audit #{a2['id']} {a2['ts']}: {a2['details'][:160]}")

# Recompute June so the MassMutual fix is reflected immediately
recompute_month("ynab_helper.db", "2026-06")
print("\nrecompute_month 2026-06 done")
