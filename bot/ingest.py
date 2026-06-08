"""Phase 3 — single entry point for turning a parsed email into ledger state.

  ingest_signal(db_path, *, signal_kind, email_id, parsed, user_id, settings)

What it does:

  * If ``signal_kind`` is a balance summary (bank's daily snapshot of cleared
    balances), write rows into ``account_balance_observed``. No ``ledger_txn``
    work — the reconciler uses these to verify the ledger sum matches the
    bank.

  * If it's a transaction signal (CC alert, order confirmation, retailer
    receipt, Venmo payment), look up any existing ``ledger_txn`` row that
    plausibly already represents this same charge (same |amount| within ±2
    days). If found, attach a new ``ledger_signal`` row pointing at it and
    enrich the payee/memo if the new signal has richer text — but do NOT
    create a duplicate ``ledger_txn``. If not found, create a fresh
    ``ledger_txn`` and categorize it with the hardened LLM + priors.

The dedupe layer is what lets multiple signals (CC alert + order confirm +
shipment tracking) collapse onto one ledger row instead of triple-counting
a $42 grocery run.

This module is the future system-of-record write path. The legacy
``bot/gmail_watcher.py`` flow that writes ``pending_order`` is still alive
and untouched — those two paths run side-by-side until Phase 7 cutover.
Callers that want both behaviors can hold their own ``insert_pending_order``
call and ``ingest_signal`` call separately.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any

from bot import storage
from bot.categorizer import Categorizer
from bot.config import Settings

log = logging.getLogger(__name__)


# Signal kinds we recognize. Each maps to where the data lands.
_TXN_KINDS = {
    "amazon_order", "amazon_shipment", "venmo_payment", "retailer_order",
    "chase_alert", "citi_alert", "coastal_transaction_alert",
    "coastal_check_cleared",
    # ynab_sync: a YNAB-side charge mirrored into the ledger via
    # ynab_watcher.poll_once. Phase 3.3 — sets up Phase 7 cutover.
    "ynab_sync",
}
_BALANCE_KINDS = {
    "balance_summary",
    "coastal_balance_summary",
    "chase_balance_summary",
}

# Signal kinds that should ALSO write a pending_txn row when ingest
# creates a new ledger_txn — so the user gets a confirm-category DM
# right after the email arrives, not silently absorbed into the ledger
# with whatever the LLM picked. ynab_sync is intentionally NOT in here —
# ynab_watcher.poll_once already writes its own pending_txn for those.
_PROMPT_USER_KINDS = {
    "chase_alert", "citi_alert", "coastal_transaction_alert",
    "coastal_check_cleared",
}


def ingest_signal(
    db_path: Path | str,
    *,
    signal_kind: str,
    email_id: str,
    parsed: dict[str, Any],
    user_id: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Apply a parsed email signal to the ledger. Returns:

        {
          "action": "new" | "merged_existing" | "balance" | "skipped",
          "ledger_txn_id": int | None,
          "ledger_signal_id": int | None,
          "category_id": str | None,    # only when action="new"
        }

    Safe to call multiple times with the same ``email_id`` — the
    ``ledger_signal`` UNIQUE(signal_kind, email_id) constraint makes
    it idempotent on the signal side.
    """
    if signal_kind in _BALANCE_KINDS:
        return _ingest_balance(db_path, email_id=email_id, parsed=parsed)

    if signal_kind not in _TXN_KINDS:
        log.warning("ingest_signal: unknown signal_kind %r", signal_kind)
        return {"action": "skipped", "ledger_txn_id": None,
                "ledger_signal_id": None, "category_id": None}

    posted_date = _coerce_date(
        parsed.get("posted_date") or parsed.get("order_date")
        or parsed.get("posted_at")
    )
    amount_cents = int(
        parsed.get("amount_cents") or parsed.get("total_cents") or 0
    )
    payee = _extract_payee(parsed)
    account_id = _resolve_account_id(db_path, parsed)
    if not posted_date or not amount_cents or not account_id:
        log.warning(
            "ingest_signal[%s] missing required fields: date=%s amt=%s acct=%s",
            signal_kind, posted_date, amount_cents, account_id,
        )
        return {"action": "skipped", "ledger_txn_id": None,
                "ledger_signal_id": None, "category_id": None}

    existing = _find_dedupe_match(
        db_path, account_id=account_id, posted_date=posted_date,
        amount_cents=amount_cents,
    )

    category_id: str | None = None
    if existing:
        ledger_txn_id = existing["id"]
        # Enrich existing row if the new signal carries better payee/memo
        _maybe_enrich(db_path, ledger_txn_id, existing, parsed, payee)
        action = "merged_existing"
    else:
        # New ledger_txn — categorize it.
        if settings is not None:
            category_id = _categorize(
                db_path, settings,
                summary=parsed.get("summary") or payee or "",
                amount_cents=amount_cents,
                date_str=str(posted_date),
                source=signal_kind,
                payee=payee,
            )
        with storage.connect(db_path) as con:
            cur = con.execute(
                """INSERT INTO ledger_txn
                     (account_id, posted_date, amount_cents, payee, memo,
                      category_id, cleared, source_signal, source_email_id,
                      dedupe_key)
                   VALUES (?, ?, ?, ?, ?, ?, 'uncleared', ?, ?, ?)""",
                (account_id, posted_date, amount_cents, payee,
                 parsed.get("memo") or parsed.get("summary") or "",
                 category_id, signal_kind, email_id,
                 _dedupe_key(account_id, posted_date, amount_cents)),
            )
            ledger_txn_id = cur.lastrowid
        action = "new"

    # Attach the signal row (idempotent on UNIQUE(signal_kind, email_id))
    ledger_signal_id: int | None = None
    try:
        with storage.connect(db_path) as con:
            cur = con.execute(
                """INSERT INTO ledger_signal
                     (ledger_txn_id, signal_kind, email_id, parsed_payload)
                   VALUES (?, ?, ?, ?)""",
                (ledger_txn_id, signal_kind, email_id, json.dumps(parsed, default=str)),
            )
            ledger_signal_id = cur.lastrowid
    except sqlite3.IntegrityError:
        # Already filed this exact signal — fine, treat as duplicate
        log.debug("ingest_signal: duplicate ledger_signal for %s/%s",
                  signal_kind, email_id)

    storage.audit(db_path, "ledger_ingest", {
        "action": action, "signal_kind": signal_kind,
        "ledger_txn_id": ledger_txn_id, "account_id": account_id,
        "amount_cents": amount_cents, "posted_date": str(posted_date),
    })

    # If this is a CC/bank charge from an email AND we just created a fresh
    # ledger_txn for it, also enqueue a pending_txn so the bot's push loop
    # surfaces it for confirm. ynab_txn_id is synthesized as "ledger:<id>"
    # so the row is uniquely keyed. _apply_choice detects that prefix and
    # skips the YNAB write (no YNAB id yet).
    pending_txn_id: int | None = None
    if action == "new" and signal_kind in _PROMPT_USER_KINDS and user_id:
        try:
            pending_txn_id = storage.insert_pending_txn(
                db_path,
                user_id=user_id,
                ynab_txn_id=f"ledger:{ledger_txn_id}",
                ynab_account_id=account_id,
                payee=payee,
                amount_cents=amount_cents,
                txn_date=posted_date,
                memo=parsed.get("summary") or parsed.get("memo") or "",
            )
            if pending_txn_id and category_id:
                with storage.connect(db_path) as con:
                    con.execute(
                        "UPDATE pending_txn SET suggested_category = ?, "
                        "raw_summary = ? WHERE id = ?",
                        (category_id, parsed.get("summary") or "",
                         pending_txn_id),
                    )
        except sqlite3.IntegrityError:
            # Already enqueued for this ledger_txn — fine
            pass

    return {
        "action": action,
        "ledger_txn_id": ledger_txn_id,
        "ledger_signal_id": ledger_signal_id,
        "category_id": category_id,
        "pending_txn_id": pending_txn_id,
    }


def _ingest_balance(db_path: Path | str, *, email_id: str,
                    parsed: dict[str, Any]) -> dict[str, Any]:
    """Write account_balance_observed rows from a balance-summary parse."""
    balances = parsed.get("account_balances") or []
    as_of = _coerce_date(parsed.get("as_of_date"))
    count = 0
    for b in balances:
        account_id = _resolve_account_id(db_path, b)
        if not account_id or not as_of:
            continue
        bcents = int(b.get("balance_cents") or 0)
        with storage.connect(db_path) as con:
            con.execute(
                """INSERT INTO account_balance_observed
                     (account_id, as_of_date, balance_cents, source_email_id)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(account_id, as_of_date) DO UPDATE SET
                     balance_cents = excluded.balance_cents,
                     source_email_id = excluded.source_email_id,
                     observed_at = CURRENT_TIMESTAMP""",
                (account_id, as_of, bcents, email_id),
            )
        count += 1
    storage.audit(db_path, "balance_observed_ingest",
                  {"email_id": email_id, "count": count,
                   "as_of_date": str(as_of)})
    return {"action": "balance", "ledger_txn_id": None,
            "ledger_signal_id": None, "category_id": None, "count": count}


def _coerce_date(v) -> date | None:
    if v is None:
        return None
    if isinstance(v, date):
        return v
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, str):
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            return None
    return None


def _extract_payee(parsed: dict) -> str:
    return (
        parsed.get("payee")
        or parsed.get("counterparty")
        or parsed.get("merchant")
        or parsed.get("source")
        or ""
    )[:120]


def _resolve_account_id(db_path: Path | str, parsed: dict) -> str | None:
    """Look up our local account.id from a parsed last4 or ynab id.

    Parsers carry account info as either ``account_id`` (already resolved),
    ``ynab_account_id``, or ``account_last4`` (a CC alert tells you the last
    4 digits of the card). For last4, match against the local account name.
    """
    direct = parsed.get("account_id")
    if direct:
        return direct
    ynab_id = parsed.get("ynab_account_id")
    if ynab_id:
        with storage.connect(db_path) as con:
            row = con.execute(
                "SELECT id FROM account WHERE ynab_account_id = ?",
                (ynab_id,),
            ).fetchone()
            if row:
                return row["id"]
    last4 = parsed.get("account_last4") or parsed.get("last4")
    if last4:
        # EXACT match on account.last4 only. Substring matching against
        # the name field was incorrectly picking up the wrong account
        # when the bank's reference number was a member-id-style shared
        # number (e.g. Coastal's "*649" appears in two YNAB accounts
        # ending in "9649"). If exact match misses, fall through to
        # label-based matching instead.
        with storage.connect(db_path) as con:
            row = con.execute(
                "SELECT id FROM account WHERE closed=0 AND last4 = ?",
                (last4,),
            ).fetchone()
            if row:
                return row["id"]
    # Coastal balance summaries and similar give us an account label
    # like "JOINT CHECKING" rather than a real last4. Match — in priority
    # order — against name + closed=0 accounts only:
    #   1. Exact label match (case-insensitive, ignoring whitespace)
    #   2. Alias table for bank-name → YNAB-name mismatches
    #   3. "<label> -" prefix match — handles
    #      "JOINT CHECKING - **********9649" reliably without matching
    #      "OLD Joint Checking" or "JOINT CHECKING - JOINT CHECKING"
    #   4. Substring fallback (single-result only — multi-match means
    #      we'd guess wrong)
    label = parsed.get("account_label") or parsed.get("account_name")
    if label:
        _LABEL_ALIASES = {
            "SPECIAL SAVINGS": "Rainy Day Savings",
            # Coastal account-name → YNAB-name translations go here.
        }
        norm = label.strip().upper()
        with storage.connect(db_path) as con:
            # 1. Exact
            row = con.execute(
                "SELECT id FROM account WHERE closed=0 AND UPPER(name) = ?",
                (norm,),
            ).fetchone()
            if row:
                return row["id"]
            # 2. Alias exact
            if norm in _LABEL_ALIASES:
                alias = _LABEL_ALIASES[norm]
                row = con.execute(
                    "SELECT id FROM account "
                    "WHERE closed=0 AND UPPER(name) = UPPER(?)",
                    (alias,),
                ).fetchone()
                if row:
                    return row["id"]
            # 3. "<label> -" prefix
            row = con.execute(
                "SELECT id FROM account "
                "WHERE closed=0 AND UPPER(name) LIKE UPPER(?)",
                (f"{label} -%",),
            ).fetchone()
            if row:
                return row["id"]
            # 4. Substring fallback — but ONLY if uniquely matched.
            rows = con.execute(
                "SELECT id FROM account "
                "WHERE closed=0 AND UPPER(name) LIKE UPPER(?)",
                (f"%{label}%",),
            ).fetchall()
            if len(rows) == 1:
                return rows[0]["id"]
    return None


def _dedupe_key(account_id: str, posted_date: date, amount_cents: int) -> str:
    return f"{account_id}|{posted_date.isoformat()}|{abs(int(amount_cents))}"


def _find_dedupe_match(
    db_path: Path | str, *, account_id: str, posted_date: date,
    amount_cents: int, window_days: int = 2,
) -> dict | None:
    """Look for an existing ledger_txn that probably represents the same charge.

    Same account, same |amount|, within ±window_days of posted_date.
    Returns the row dict or None.
    """
    with storage.connect(db_path) as con:
        row = con.execute(
            """SELECT id, posted_date, amount_cents, payee, memo
               FROM ledger_txn
               WHERE account_id = ?
                 AND ABS(amount_cents) = ABS(?)
                 AND posted_date BETWEEN date(?, ?) AND date(?, ?)
               ORDER BY ABS(julianday(posted_date) - julianday(?)) ASC
               LIMIT 1""",
            (account_id, amount_cents,
             posted_date, f"-{window_days} days",
             posted_date, f"+{window_days} days",
             posted_date),
        ).fetchone()
    return dict(row) if row else None


def _maybe_enrich(db_path: Path | str, ledger_txn_id: int, existing: dict,
                  parsed: dict, new_payee: str) -> None:
    """If the new signal has a better payee or memo, write it onto the row."""
    new_memo = parsed.get("summary") or parsed.get("memo") or ""
    updates: list[tuple[str, Any]] = []
    if new_payee and (not existing.get("payee") or
                       _is_generic_payee(existing["payee"])):
        updates.append(("payee", new_payee))
    if new_memo and (not existing.get("memo") or
                      len(new_memo) > len(existing["memo"] or "")):
        updates.append(("memo", new_memo[:500]))
    if not updates:
        return
    set_clause = ", ".join(f"{col} = ?" for col, _ in updates)
    values = [v for _, v in updates] + [ledger_txn_id]
    with storage.connect(db_path) as con:
        con.execute(
            f"UPDATE ledger_txn SET {set_clause}, updated_at = CURRENT_TIMESTAMP "
            f"WHERE id = ?",
            values,
        )


def _is_generic_payee(p: str) -> bool:
    """`AMZN`, `Amazon`, `Venmo` alone are generic — order confirms can do better."""
    return (p or "").strip().lower() in {"amazon", "amzn", "venmo", "paypal", ""}


def _categorize(
    db_path: Path | str, settings: Settings, *,
    summary: str, amount_cents: int, date_str: str, source: str, payee: str,
) -> str | None:
    """Run the hardened categorizer with priors. Returns category_id or None."""
    spending = storage.list_categories_for_spending(db_path)
    if not spending:
        return None
    cats = [{"id": c["id"], "name": c["name"], "group": c["group_name"]}
            for c in spending]
    priors = storage.get_category_priors_for_payee(db_path, payee, top_n=5)
    engine = Categorizer(
        settings.ollama.endpoint, settings.ollama.model,
        settings.ollama.temperature,
    )
    result = engine.suggest(
        summary=summary, amount_cents=amount_cents, date_str=date_str,
        source=source, categories=cats, priors=priors,
    )
    return result.get("category_id")
