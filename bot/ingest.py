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
import re
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
    "coastal_check_cleared", "paypal_payment",
    # ynab_sync: a YNAB-side charge mirrored into the ledger via
    # ynab_watcher.poll_once. Phase 3.3 — sets up Phase 7 cutover.
    "ynab_sync",
}
_BALANCE_KINDS = {
    "balance_summary",
    "coastal_balance_summary",
    "chase_balance_summary",
    "citi_balance_summary",
}

# Signal kinds that should ALSO write a pending_txn row when ingest
# creates a new ledger_txn — so the user gets a confirm-category DM
# right after the email arrives, not silently absorbed into the ledger
# with whatever the LLM picked. ynab_sync is intentionally NOT in here —
# ynab_watcher.poll_once already writes its own pending_txn for those.
_PROMPT_USER_KINDS = {
    "chase_alert", "citi_alert", "coastal_transaction_alert",
    "coastal_check_cleared", "paypal_payment",
}


def ingest_signal(
    db_path: Path | str,
    *,
    signal_kind: str,
    email_id: str,
    parsed: dict[str, Any],
    user_id: str | None = None,
    settings: Settings | None = None,
    _exact_only_dedupe: bool = False,
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

    # Idempotency guard: if a ledger_signal row already exists for this
    # (signal_kind, email_id), the bot has already processed this email.
    # Bail out early so a re-poll (gmail_watcher's mark-processed sometimes
    # fails on certain Coastal/Chase emails and the same UID re-appears in
    # the next search) doesn't create an orphan ledger_txn the way it did
    # for the $38.20 Amazon charge: the duplicate slipped past dedupe and
    # built lt#23818 alongside the original lt#23650.
    with storage.connect(db_path) as con:
        existing_sig = con.execute(
            "SELECT ledger_txn_id FROM ledger_signal "
            "WHERE signal_kind = ? AND email_id = ?",
            (signal_kind, email_id),
        ).fetchone()
    if existing_sig is not None:
        log.debug("ingest_signal: skipping repeat of %s/%s (already filed)",
                  signal_kind, email_id)
        return {
            "action": "already_filed",
            "ledger_txn_id": existing_sig["ledger_txn_id"],
            "ledger_signal_id": None, "category_id": None,
        }

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
        amount_cents=amount_cents, payee=payee,
        exact_only=_exact_only_dedupe,
    )

    # PayPal aggregator dedupe: a single PayPal email may pay for N
    # items that YNAB stores as N separate line items (the "I bought 5
    # Southwest tickets via PayPal" case). The exact-amount dedupe above
    # misses these because no single existing row matches $3,496.35.
    # Check for a cluster of smaller rows that sum to roughly our total.
    if not existing and signal_kind == "paypal_payment":
        aggregator_match = _find_aggregator_breakout(
            db_path, account_id=account_id, posted_date=posted_date,
            amount_cents=amount_cents, parsed=parsed,
        )
        if aggregator_match:
            storage.audit(db_path, "paypal_aggregator_skipped", {
                "email_id": email_id,
                "incoming_amount": amount_cents,
                "matched_row_ids": aggregator_match["matched_ids"],
                "matched_sum": aggregator_match["matched_sum"],
                "merchant": aggregator_match["merchant"],
            })
            log.info(
                "ingest_signal[paypal_payment] skipping aggregator — "
                "found %d existing rows summing $%.2f on same account ±7d "
                "for merchant %r",
                len(aggregator_match["matched_ids"]),
                aggregator_match["matched_sum"] / 100,
                aggregator_match["merchant"],
            )
            return {
                "action": "skipped_aggregator",
                "ledger_txn_id": aggregator_match["matched_ids"][0],
                "ledger_signal_id": None,
                "category_id": None,
            }

    category_id: str | None = None
    if existing:
        ledger_txn_id = existing["id"]
        # Enrich existing row if the new signal carries better payee/memo/amount
        existing["account_id"] = account_id
        existing["posted_date"] = posted_date
        _maybe_enrich(db_path, ledger_txn_id, existing, parsed, payee)
        action = "merged_existing"
    else:
        # New ledger_txn — categorize it.
        # Phase 2 enrichment: when a CC/bank alert carries a generic
        # payment-processor brand as the payee (AMAZON MKTPLACE PMTS,
        # VENMO, PAYPAL, APPLE.COM/BILL), look up the matching pending_order
        # (Amazon order confirm / Venmo memo / Apple receipt) and merge
        # its summary in. This is what tells the bot's DM that an
        # "AMAZON MKTPLACE PMTS $15.00" is actually "Rubber Golf Tees".
        # When the matched order is already user-categorized, use ITS
        # category directly so the bot doesn't double-prompt with a worse
        # LLM guess for the same purchase.
        matched_order: dict | None = None
        if signal_kind in _PROMPT_USER_KINDS and _is_generic_payee(payee):
            matched_order = _enrich_from_pending_order(db_path, parsed, payee)
            if matched_order:
                storage.audit(db_path, "ingest_enriched_from_order", {
                    "matched_order_id": matched_order.get("id"),
                    "source": matched_order.get("source"),
                    "had_chosen_category": bool(matched_order.get("chosen_category")),
                })

        if settings is not None:
            if matched_order and matched_order.get("chosen_category"):
                # Trust the user's prior choice on the matching order
                category_id = matched_order["chosen_category"]
                storage.audit(db_path, "categorize_via_matched_order", {
                    "payee": payee, "category_id": category_id,
                    "matched_order_id": matched_order.get("id"),
                })
            else:
                category_id = _categorize(
                    db_path, settings,
                    summary=parsed.get("summary") or payee or "",
                    amount_cents=amount_cents,
                    date_str=str(posted_date),
                    source=signal_kind,
                    payee=payee,
                )
        with storage.connect(db_path) as con:
            # NOTE: ledger_txn.category_id is intentionally left NULL here
            # even when the categorizer produced a high-confidence suggestion.
            # The suggestion stays on pending_txn.suggested_category and is
            # only promoted to ledger_txn.category_id when the user confirms
            # (via _apply_choice in telegram_bot.py).
            #
            # Why: month_category.activity_cents is summed from ledger_txn,
            # and the "Categorized as X. X: $Y left this month." reply
            # would otherwise show the same $Y across every confirmation in
            # the same category — because the LLM suggestion had already
            # counted each charge into activity before the user tapped.
            cur = con.execute(
                """INSERT INTO ledger_txn
                     (account_id, posted_date, amount_cents, payee, memo,
                      category_id, cleared, source_signal, source_email_id,
                      dedupe_key)
                   VALUES (?, ?, ?, ?, ?, NULL, 'uncleared', ?, ?, ?)""",
                (account_id, posted_date, amount_cents, payee,
                 parsed.get("memo") or parsed.get("summary") or "",
                 signal_kind, email_id,
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
            # Phase 7 queue routing: per Steven's design, an Amazon-
            # marketplace CC alert that did NOT find a matching order
            # email goes to HOLD so the bot doesn't DM him with a bare
            # "Chase $X.XX at AMAZON MKTPLACE PMTS" prompt. The hold
            # sweep retries enrichment every 30 min and promotes to HOT
            # once the matching order arrives — or to COLD after 24h.
            # Everything else (Citi/Coastal/Chase non-Amazon, Apple,
            # Venmo with rich memo, etc.) goes straight to HOT.
            if pending_txn_id and _is_generic_payee(payee):
                payee_upper = (payee or "").upper()
                is_amazon = ("AMAZON" in payee_upper or "AMZN" in payee_upper)
                if is_amazon and not matched_order:
                    with storage.connect(db_path) as con:
                        con.execute(
                            "UPDATE pending_txn SET queue_lane = 'hold', "
                            "lane_changed_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (pending_txn_id,),
                        )
                    storage.audit(db_path, "queue_lane_change", {
                        "pt_id": pending_txn_id,
                        "from": "hot", "to": "hold",
                        "reason": "amazon_awaiting_order_enrichment",
                    })
        except sqlite3.IntegrityError:
            # Already enqueued for this ledger_txn — fine
            pass

    # Multi-transaction emails (Coastal packs several debits into one
    # "Transaction Alert"). Fan each extra line out as its own ingest call.
    # We synthesize a per-line email_id so the ledger_signal UNIQUE
    # constraint doesn't collide on the parent email_id.
    for i, extra in enumerate(parsed.get("additional_txns") or []):
        sub_parsed = {
            "source": parsed.get("source"),
            "account_name": parsed.get("account_name"),
            "account_id": parsed.get("account_id"),
            "account_last4": parsed.get("account_last4"),
            "amount_cents": extra.get("amount_cents"),
            "type_word": extra.get("type_word"),
            "payee": extra.get("payee"),
            "merchant": extra.get("merchant"),
            "posted_date": parsed.get("posted_date"),
            "summary": (
                f"Coastal {parsed.get('account_name') or '?'} "
                f"${abs(extra.get('amount_cents') or 0)/100:.2f} "
                f"{extra.get('type_word') or '(unknown type)'}"
            ),
        }
        sub_email_id = f"{email_id}#line{i+2}"
        ingest_signal(
            db_path,
            signal_kind=signal_kind,
            email_id=sub_email_id,
            parsed=sub_parsed,
            user_id=user_id,
            settings=settings,
            # Sibling lines in the same email shouldn't fuzzy-dedupe against
            # each other (two MASSMUTUAL LIFE debits on the same day with
            # different amounts shouldn't collapse into one row). External
            # signals (a later YNAB sync) still merge cleanly via the
            # second ingest of the same exact amount.
            _exact_only_dedupe=True,
        )

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
            # Coastal account-name → YNAB-name translations go here.
            # NOTE: "SPECIAL SAVINGS" is Coastal's $5 share-required
            # membership account, NOT the Rainy Day Savings at the
            # high-yield bank. Don't alias it — let it skip resolution.
            # "BASIC CHECKING" is what Coastal labels the account YNAB
            # tracks as "BUSINESS CHECKING - **********9649".
            "BASIC CHECKING": "BUSINESS CHECKING - **********9649",
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


def _find_aggregator_breakout(
    db_path: Path | str, *,
    account_id: str,
    posted_date: date,
    amount_cents: int,
    parsed: dict,
    window_days: int = 7,
    min_children: int = 2,
    sum_ratio_low: float = 0.85,
    sum_ratio_high: float = 1.15,
) -> dict | None:
    """Detect that a PayPal aggregate payment is already covered by
    multiple smaller existing rows (the YNAB-split-into-line-items case).

    Returns ``{matched_ids: [...], matched_sum: int, merchant: str}``
    if found, else None.

    Heuristic:
      * Find rows on same account, ±window_days from posted_date.
      * Extract the merchant from parsed memo (PayPal memos follow
        'payment to MERCHANT on YYYY-MM-DD').
      * Keep only rows whose payee contains the first merchant token.
      * If 2+ rows sum to within [85%, 115%] of the incoming amount,
        that's an aggregator breakout.
    """
    import re as _re
    memo = (parsed.get("memo") or "").strip()
    summary = (parsed.get("summary") or "").strip()
    haystack = f"{memo} {summary}"
    m = _re.search(r"payment to ([\w &\.\'-]+?)(?:\s+on\s+\d|\s*$)", haystack, _re.I)
    if not m:
        return None
    merchant = m.group(1).strip()
    merchant_token = merchant.split()[0].lower() if merchant else ""
    if len(merchant_token) < 4:
        # Token too generic ('the', 'inc') to be a discriminator.
        return None

    incoming_abs = abs(int(amount_cents))
    with storage.connect(db_path) as con:
        rows = con.execute(
            """SELECT id, posted_date, amount_cents, payee
               FROM ledger_txn
               WHERE account_id = ?
                 AND amount_cents < 0
                 AND ABS(julianday(posted_date) - julianday(?)) <= ?
                 AND LOWER(COALESCE(payee, '')) LIKE ?""",
            (account_id, posted_date, window_days,
             f"%{merchant_token}%"),
        ).fetchall()
    if len(rows) < min_children:
        return None
    matched_sum = sum(-r["amount_cents"] for r in rows)
    if matched_sum == 0:
        return None
    ratio = matched_sum / incoming_abs if incoming_abs > 0 else 0
    if not (sum_ratio_low <= ratio <= sum_ratio_high):
        return None
    return {
        "matched_ids": [r["id"] for r in rows],
        "matched_sum": matched_sum,
        "merchant": merchant,
    }


def _dedupe_key(account_id: str, posted_date: date, amount_cents: int) -> str:
    return f"{account_id}|{posted_date.isoformat()}|{abs(int(amount_cents))}"


def _find_dedupe_match(
    db_path: Path | str, *, account_id: str, posted_date: date,
    amount_cents: int, window_days: int = 2, payee: str | None = None,
    exact_only: bool = False,
) -> dict | None:
    """Look for an existing ledger_txn that probably represents the same charge.

    Two passes:
      1. EXACT — same account, |amount| matches, within ±window_days.
      2. TIP-TOLERANT — same account, ±window_days, same payee prefix,
         |amount| within +0% to +40% of either direction. Catches the
         restaurant case where a Citi instant alert fires at the pre-auth
         amount ($16.54) and YNAB later syncs the post-tip settled amount
         ($19.54). Both refer to the same real-world transaction.

    Returns the row dict or None. Caller can tell which pass hit by
    comparing returned amount to the incoming amount.
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
        if row:
            return dict(row)
        if exact_only:
            return None

        # Fuzzy pass — require a payee + a non-trivial prefix so we don't
        # collide unrelated charges of similar amount.
        if not payee:
            return None
        first_tokens = payee.lower().strip().split()
        if not first_tokens:
            return None
        # Strip generic banking lead-words so the discriminator is the
        # actual MERCHANT name. Without this, "Deposit ACH SMITH SINNETT"
        # and "Deposit ACH CITICARDS CASH" both reduce to "deposit ach"
        # and collide — caused a real Citi-rewards-tagged-as-Allison-paycheck
        # bug 2026-06-17.
        _GENERIC_BANK_WORDS = {
            "deposit", "withdrawal", "ach", "check", "purchase",
            "debit", "credit", "pos", "atm", "transfer",
        }
        first_tokens = [t for t in first_tokens if t not in _GENERIC_BANK_WORDS]
        if not first_tokens:
            return None
        prefix = " ".join(first_tokens[:2]) if len(first_tokens) >= 2 else first_tokens[0]
        if len(prefix) < 4:  # too short to be discriminative
            return None
        pattern = f"{prefix}%"
        amt = abs(int(amount_cents))
        row = con.execute(
            """SELECT id, posted_date, amount_cents, payee, memo
               FROM ledger_txn
               WHERE account_id = ?
                 AND posted_date BETWEEN date(?, ?) AND date(?, ?)
                 AND LOWER(payee) LIKE ?
                 AND (
                   (ABS(amount_cents) BETWEEN ? AND ?)
                   OR (? BETWEEN ABS(amount_cents) AND CAST(ABS(amount_cents) * 1.4 AS INTEGER))
                 )
               ORDER BY ABS(julianday(posted_date) - julianday(?)) ASC,
                        ABS(ABS(amount_cents) - ?) ASC
               LIMIT 1""",
            (account_id,
             posted_date, f"-{window_days} days",
             posted_date, f"+{window_days} days",
             pattern,
             amt, int(amt * 1.4),     # existing amount within ±40% of new
             amt,                      # new amount within ±40% of existing
             posted_date,
             amt),
        ).fetchone()
    return dict(row) if row else None


def _maybe_enrich(db_path: Path | str, ledger_txn_id: int, existing: dict,
                  parsed: dict, new_payee: str) -> None:
    """If the new signal has a better payee, memo, or settled amount, write it.

    Settled-amount upgrade: when the incoming signal carries a LARGER amount
    than the existing row by up to +40% (and same payee prefix per the
    fuzzy-dedupe match), assume the existing row was a pre-auth and this is
    the post-tip settled amount. Update the row's amount + dedupe_key so
    envelope math reflects what was actually charged.
    """
    new_memo = parsed.get("summary") or parsed.get("memo") or ""
    new_amount = int(parsed.get("amount_cents") or parsed.get("total_cents") or 0)
    existing_amount = int(existing.get("amount_cents") or 0)

    updates: list[tuple[str, Any]] = []
    if new_payee and (not existing.get("payee") or
                       _is_generic_payee(existing["payee"])):
        updates.append(("payee", new_payee))
    if new_memo and (not existing.get("memo") or
                      len(new_memo) > len(existing["memo"] or "")):
        updates.append(("memo", new_memo[:500]))

    # Settled-amount upgrade: incoming bigger by up to 40% → assume tip.
    # OUTFLOWS ONLY — tips don't apply to deposits. And same-date only —
    # a 2-day gap is not a tip settlement, it's a coincidence of amount.
    # Without these guards, a Citi rewards $3020 ACH on 6/17 got merged
    # into Allison's Smith Sinnett $2465 paycheck on 6/15 because the
    # generic "Deposit ACH" prefix matched (now also stripped above) and
    # 3020/2465 = 1.22 is within the 40% tip tolerance.
    existing_is_outflow = (existing.get("amount_cents") or 0) < 0
    same_date = str(existing.get("posted_date") or "") == str(
        parsed.get("posted_date") or ""
    )
    if (new_amount and existing_amount
            and existing_is_outflow
            and same_date
            and abs(new_amount) > abs(existing_amount)
            and abs(new_amount) <= int(abs(existing_amount) * 1.4)):
        updates.append(("amount_cents", new_amount))
        # dedupe_key embeds amount; refresh so future exact-match lookups
        # against the settled amount find this row.
        new_dedupe = _dedupe_key(
            existing["account_id"],            # injected by caller before this
            existing["posted_date"],           # call (so we don't need to re-derive)
            new_amount,
        )
        updates.append(("dedupe_key", new_dedupe))
        log.info(
            "ledger_txn %s amount upgraded %s -> %s (tip on %s)",
            ledger_txn_id, existing_amount, new_amount,
            (existing.get("payee") or "")[:30],
        )

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


def _enrich_from_pending_order(
    db_path: Path | str, parsed: dict[str, Any], payee: str,
) -> dict | None:
    """For CC-alert ingests with a generic payee (AMAZON MKTPLACE PMTS,
    VENMO, etc.), look for the matching pending_order and merge its
    raw_summary into ``parsed["summary"]`` so the downstream categorizer
    and the user-facing DM see the rich item-level context, not just the
    bare merchant string.

    Returns the matched pending_order dict, or None when no confident
    match is found. Caller can use the matched order's chosen_category
    (when present) as the suggestion, skipping the LLM entirely.

    Uses the same scoring function (``bot.matcher.match_score``) that the
    YNAB-sync-time matcher uses, so the bot's two enrichment paths agree
    on what counts as a match.
    """
    from bot.matcher import _PAYEE_PATTERNS, find_best_match
    if not payee:
        return None
    source: str | None = None
    for src, pat in _PAYEE_PATTERNS.items():
        if pat.search(payee):
            source = src
            break
    if source is None:
        return None

    posted = _coerce_date(
        parsed.get("posted_date") or parsed.get("order_date")
    )
    amount_cents = int(
        parsed.get("amount_cents") or parsed.get("total_cents") or 0
    )
    if not posted or not amount_cents:
        return None

    # Pull pending_orders of this source from the last 21 days regardless
    # of status — even an 'expired' order's summary is useful for the user
    # to see what they bought, even if it didn't auto-categorize.
    with storage.connect(db_path) as con:
        rows = con.execute(
            """SELECT * FROM pending_order
               WHERE source = ?
                 AND order_date >= date(?, '-21 days')""",
            (source, str(posted)),
        ).fetchall()
    candidates = [dict(r) for r in rows]
    if not candidates:
        return None

    txn_for_score = {
        # match_score expects outflow as negative; CC alerts already are
        "amount_cents": -abs(amount_cents),
        "txn_date": posted,
        "payee": payee,
        "memo": parsed.get("memo") or parsed.get("summary") or "",
    }
    best = find_best_match(candidates, txn_for_score, source=source)
    if not best:
        return None

    order_summary = (best.get("raw_summary") or "").strip()
    if order_summary:
        ext_id = best.get("external_id") or ""
        if ext_id:
            new_summary = f"{source.title()} order {ext_id}: {order_summary}"
        else:
            new_summary = f"{source.title()}: {order_summary}"
        parsed["summary"] = new_summary
    return best


_GENERIC_PAYEE_RE = re.compile(
    r"^(?:"
    r"amazon|amzn|venmo|paypal|"             # bare brand
    r"amazon\.com|amzn\s*mktp|amazon\s+mktplace|"  # Amazon CC-alert formats
    r"venmo\s*\*|paypal\s*\*|"               # processor prefixes
    r"apple\.?\s*com|apl\s*\*"               # Apple CC-alert formats
    r")\b",
    re.IGNORECASE,
)


def _is_generic_payee(p: str) -> bool:
    """A payee that's a payment-processor brand, not a useful merchant identity.

    These are the cases where the bot has a richer enrichment signal somewhere
    (Amazon order confirm, Venmo memo, PayPal subject) — we should rely on
    that, not on shaky priors. Examples: `AMAZON MKTPLACE PMTS`,
    `AMZN MKTP US*1A2B3`, `VENMO *DESC`, `PAYPAL *MERCHANT`, plain `Amazon`.
    """
    s = (p or "").strip()
    if not s:
        return True
    return bool(_GENERIC_PAYEE_RE.match(s))


def _categorize(
    db_path: Path | str, settings: Settings, *,
    summary: str, amount_cents: int, date_str: str, source: str, payee: str,
) -> str | None:
    """Return a suggested category_id, or None.

    Resolution order (first hit wins):
      1. Hard-coded payee override map (bot.payee_overrides) — catches
         recurring bills like AT&T → "Cell Phone (4th)" that the LLM
         can't pick because the named-bill envelopes are deliberately
         excluded from the spending-only candidate pool.
      2. Strongest historical prior — if Steven has categorized this
         payee to the same category ≥3 times AND that category accounts
         for ≥70% of his history, just use that. Lets non-spending
         priors (bills, savings) win without going through the LLM.
      3. The hardened LLM categorizer, restricted to spending categories
         and softly biased by priors (still spending-only for the LLM's
         restricted candidate list).
    """
    # 1. Explicit override map
    from bot.payee_overrides import resolve_payee_override
    override = resolve_payee_override(db_path, payee)
    if override:
        storage.audit(db_path, "categorize_via_override", {
            "payee": payee, "category_id": override["category_id"],
            "category_name": override["category_name"],
            "rule": override.get("rule"),
        })
        return override["category_id"]

    # 2. Strong historical prior (bypasses LLM + is_spending filter)
    # — skip for payment-processor brands (Amazon, Venmo, PayPal, etc.)
    # because the canonical signal lives in the matching order/payment
    # email; trusting a small-sample prior here misroutes the charge.
    if _is_generic_payee(payee):
        strong = None
    else:
        strong = storage.get_strongest_payee_category(db_path, payee)
    if strong:
        storage.audit(db_path, "categorize_via_prior", {
            "payee": payee, "category_id": strong["category_id"],
            "category_name": strong["category_name"],
            "pct": strong["pct"], "count": strong["count"],
        })
        return strong["category_id"]

    # 3. Fall through to the LLM with spending-only candidates + priors hint
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
