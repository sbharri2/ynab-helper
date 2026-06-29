"""Force a new ledger_txn ingest and see what category_id is when the
UPDATE runs."""
from __future__ import annotations
import logging
from datetime import date

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

# Monkey-patch to inspect category_id before the UPDATE
import bot.ingest as ingest_module
import bot.storage as storage_module

orig_insert_pending = storage_module.insert_pending_txn
def spy_insert(*args, **kwargs):
    result = orig_insert_pending(*args, **kwargs)
    print(f"  >>> storage.insert_pending_txn returned: {result}")
    return result
storage_module.insert_pending_txn = spy_insert
ingest_module.storage.insert_pending_txn = spy_insert  # in case of import-time alias

orig_resolve = None
from bot import payee_overrides
orig_resolve = payee_overrides.resolve_payee_override
def spy_resolve(*args, **kwargs):
    result = orig_resolve(*args, **kwargs)
    print(f"  >>> resolve_payee_override returned: {result}")
    return result
payee_overrides.resolve_payee_override = spy_resolve

from bot.config import load_settings
settings = load_settings()
DB = "ynab_helper.db"

# Use an amount/date that absolutely doesn't exist
import random
unique_cents = -(random.randint(900000, 999999))
print(f"Using unique amount_cents={unique_cents}")

import uuid
parsed = {
    "source": "citi_alert",
    "amount_cents": unique_cents,
    "payee": "Amica Mutual Ins SPYTEST",
    "merchant": "Amica Mutual Ins",
    "account_last4": "5674",
    "posted_date": date(2026, 6, 26),
    "summary": f"Citi DC ${abs(unique_cents)/100:.2f} at Amica Mutual Ins SPYTEST",
}

result = ingest_module.ingest_signal(
    DB,
    signal_kind="citi_alert",
    email_id=f"spy-test-{uuid.uuid4()}",
    parsed=parsed,
    user_id="steven",
    settings=settings,
)
print(f"\nFinal result: {result}")
pt_id = result.get("pending_txn_id")
if pt_id:
    import sqlite3
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    r = con.execute(
        "SELECT id, suggested_category, raw_summary FROM pending_txn WHERE id = ?",
        (pt_id,)).fetchone()
    print(f"\nFinal pt row: {dict(r)}")
    # cleanup
    con.execute("DELETE FROM pending_txn WHERE id = ?", (pt_id,))
    con.execute("DELETE FROM ledger_signal WHERE ledger_txn_id = ?",
                 (result.get("ledger_txn_id"),))
    con.execute("DELETE FROM ledger_txn WHERE id = ?",
                 (result.get("ledger_txn_id"),))
    con.commit()
    print("(cleaned up test rows)")
