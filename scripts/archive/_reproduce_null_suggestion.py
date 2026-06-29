"""Reproduce the 'override fires but suggestion stays NULL' bug.

Calls ingest_signal directly with an Amica-like Citi alert payload
and checks every step.
"""
from __future__ import annotations
import logging
from datetime import date

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s %(message)s")

from bot import storage
from bot.config import load_settings
from bot.ingest import ingest_signal

settings = load_settings()
DB = "ynab_helper.db"

# Fake an Amica Citi alert. Use a unique email_id so we don't collide.
import uuid
test_email_id = f"test-amica-{uuid.uuid4()}"

parsed = {
    "source": "citi_alert",
    "amount_cents": -40490,  # Note: positive (CC alerts pass positive)
    "payee": "Test Amica Mutual Ins Lincoln USA",
    "merchant": "Amica Mutual Ins",
    "account_last4": "5674",
    "posted_date": date(2026, 6, 26),
    "summary": "Citi DC $404.90 at Amica Mutual Ins Lincoln USA on 2026-06-26",
}

print("Before:")
with storage.connect(DB) as con:
    print(f"  pending_txn count: {con.execute('SELECT COUNT(*) FROM pending_txn').fetchone()[0]}")

result = ingest_signal(
    DB,
    signal_kind="citi_alert",
    email_id=test_email_id,
    parsed=parsed,
    user_id="steven",
    settings=settings,
)
print(f"\nResult: {result}")

# Check the row
pt_id = result.get("pending_txn_id")
if pt_id:
    with storage.connect(DB) as con:
        row = con.execute(
            "SELECT id, payee, amount_cents, suggested_category, "
            "       queue_lane, raw_summary "
            "FROM pending_txn WHERE id = ?", (pt_id,),
        ).fetchone()
        print(f"\nNew pending_txn:")
        for k, v in dict(row).items():
            print(f"  {k} = {v!r}")

print("\nResult dict category_id:", result.get("category_id"))
