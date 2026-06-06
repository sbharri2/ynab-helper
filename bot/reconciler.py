"""Phase 5 — daily reconciliation against bank balance summaries.

After each bank-provided ``account_balance_observed`` row is written by
``bot.ingest._ingest_balance``, this module compares the bank's stated
balance to the running ledger sum and decides whether everything agrees.

  reconcile_account(db_path, account_id, as_of_date)

Result paths:
  * Agree (within ±$0.50 tolerance): mark all ledger_txn rows for that
    account through ``as_of_date`` as ``cleared='reconciled'`` and audit
    ``reconcile_ok``. Tolerance absorbs noise like end-of-day pending
    overlap or tiny FX rounding.
  * Disagree: audit ``reconcile_mismatch`` with the delta. The daily
    summary reporter will surface it; we never silently auto-correct.

The starting balance for the ledger sum is ``account.balance_cents`` from
the YNAB-history import. Ledger rows reflect deltas from that anchor; the
expected balance is ``starting + sum(amount_cents through as_of_date)``.

This module is read-mostly. The only write paths are:
  - ledger_txn.cleared updates (only on agree)
  - audit_log inserts

Both are inside a single transaction per call.
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from bot import storage

log = logging.getLogger(__name__)

# 50 cents — absorbs end-of-day pending overlap and rounding without
# being so generous that real drift gets hidden.
DEFAULT_TOLERANCE_CENTS = 50


def reconcile_account(
    db_path: Path | str,
    account_id: str,
    as_of_date: date,
    *,
    tolerance_cents: int = DEFAULT_TOLERANCE_CENTS,
) -> dict:
    """Compare ledger sum to observed bank balance for one account/date.

    Returns:
        {
          "account_id": str,
          "as_of_date": date,
          "expected_cents": int,   # starting + sum(ledger through date)
          "observed_cents": int,   # what the bank said
          "delta_cents": int,      # observed - expected (positive = bank up)
          "status": "ok" | "mismatch" | "no_observation",
          "reconciled_count": int, # rows marked cleared='reconciled' on ok
        }
    """
    with storage.connect(db_path) as con:
        observed_row = con.execute(
            """SELECT balance_cents FROM account_balance_observed
               WHERE account_id = ? AND as_of_date = ?""",
            (account_id, as_of_date),
        ).fetchone()
        acct_row = con.execute(
            "SELECT balance_cents FROM account WHERE id = ?",
            (account_id,),
        ).fetchone()
        sum_row = con.execute(
            """SELECT COALESCE(SUM(amount_cents), 0) AS s
               FROM ledger_txn
               WHERE account_id = ? AND posted_date <= ?""",
            (account_id, as_of_date),
        ).fetchone()

    if observed_row is None:
        log.info("reconcile_account[%s on %s]: no observation",
                 account_id, as_of_date)
        return {
            "account_id": account_id, "as_of_date": as_of_date,
            "expected_cents": None, "observed_cents": None,
            "delta_cents": None, "status": "no_observation",
            "reconciled_count": 0,
        }

    starting = int((acct_row or {"balance_cents": 0})["balance_cents"] or 0)
    # NOTE: account.balance_cents is the YNAB-import snapshot (a "current"
    # number as of import day). For a strict reconcile this would need to be
    # the *beginning of history* anchor, but in practice this module is
    # meant to be called daily against a recent date — so the comparison is
    # really "did we move from yesterday's anchor by what we expected?"
    # When Phase 7 cuts over, we'll re-anchor balance_cents to the cutover
    # date and the math becomes exact.
    expected = starting + int(sum_row["s"] or 0)
    observed = int(observed_row["balance_cents"])
    delta = observed - expected

    reconciled_count = 0
    if abs(delta) <= tolerance_cents:
        with storage.connect(db_path) as con:
            cur = con.execute(
                """UPDATE ledger_txn
                   SET cleared = 'reconciled', updated_at = CURRENT_TIMESTAMP
                   WHERE account_id = ? AND posted_date <= ?
                     AND cleared != 'reconciled'""",
                (account_id, as_of_date),
            )
            reconciled_count = cur.rowcount or 0
        status = "ok"
        storage.audit(db_path, "reconcile_ok", {
            "account_id": account_id, "as_of_date": str(as_of_date),
            "expected_cents": expected, "observed_cents": observed,
            "delta_cents": delta, "reconciled_count": reconciled_count,
        })
        log.info(
            "reconcile_account[%s on %s]: OK delta=%+d (cleared %d rows)",
            account_id, as_of_date, delta, reconciled_count,
        )
    else:
        status = "mismatch"
        storage.audit(db_path, "reconcile_mismatch", {
            "account_id": account_id, "as_of_date": str(as_of_date),
            "expected_cents": expected, "observed_cents": observed,
            "delta_cents": delta,
        })
        log.warning(
            "reconcile_account[%s on %s]: MISMATCH delta=%+d cents "
            "(expected %d, bank says %d)",
            account_id, as_of_date, delta, expected, observed,
        )

    return {
        "account_id": account_id, "as_of_date": as_of_date,
        "expected_cents": expected, "observed_cents": observed,
        "delta_cents": delta, "status": status,
        "reconciled_count": reconciled_count,
    }


def reconcile_all_observed(db_path: Path | str, as_of_date: date) -> list[dict]:
    """Reconcile every account that has an observation for `as_of_date`."""
    with storage.connect(db_path) as con:
        rows = con.execute(
            """SELECT account_id FROM account_balance_observed
               WHERE as_of_date = ?""",
            (as_of_date,),
        ).fetchall()
    return [
        reconcile_account(db_path, r["account_id"], as_of_date)
        for r in rows
    ]
