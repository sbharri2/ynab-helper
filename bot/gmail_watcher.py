"""Multi-source Gmail poller.

For each (gmail_account x email_source) pair:
  1. Search Gmail for matching new messages
  2. Parse each with the source-specific parser
  3. Insert into pending_order (idempotent on email_id)
  4. Ask the categorizer for a suggestion
  5. Persist suggestion on the row

The Telegram bot independently watches for new pending_order rows and pushes
them to the user - this module does not talk to Telegram directly.

Auth backend: IMAP with App Password (preferred) or legacy Gmail OAuth.
IMAP wins when `imap_password_env` is set on the GmailAccount config and
the named env var has a value. The OAuth path is kept as a fallback so
already-running installs that haven't migrated still work.
"""
from __future__ import annotations

import base64
import importlib
import logging
import os
from datetime import date

import sqlite3

from bot import storage
from bot.config import Settings
from bot.categorizer import Categorizer
from bot.ynab_client import YnabClient

log = logging.getLogger(__name__)


def _build_gmail_service(token_path: str):
    """Legacy OAuth path. Only called when an account isn't configured
    for IMAP. Imported lazily so machines without google-api-python-client
    installed can still run the IMAP path.
    """
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    token_path = os.path.expanduser(token_path)
    creds = Credentials.from_authorized_user_file(token_path)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(token_path, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# YNAB's canonical "Inflow: Ready to Assign" category id. Mirrored locally
# from import_ynab_history. Venmo inflows route here automatically; the
# user shouldn't be prompted to categorize income as a spending category.
_INFLOW_READY_TO_ASSIGN_ID = "1fbde6be-7144-4752-ae25-6afa80b09f95"


def _account_imap_password(account) -> str | None:
    """Resolve the App Password for an account from its env var name."""
    env_name = getattr(account, "imap_password_env", "")
    if not env_name:
        return None
    return os.environ.get(env_name)


def _person_from_recipient(headers: dict, default_user_id: str) -> str:
    """Attribute an order to whoever it was addressed to.

    Steven's inbox receives both his own Amazon confirmations AND Allison's
    (she forwards hers, and the forward preserves the original To/Delivered-To).
    Reading the recipient — not the polled mailbox — is what lets the per-person
    Amazon buckets split Steven vs Allison correctly. Falls back to the polling
    account's user_id when the recipient is unrecognized.
    """
    to = ((headers.get("To") or "") + " "
          + (headers.get("Delivered-To") or "")).lower()
    if "allison" in to:
        return "allison"
    if "sbharri2" in to or "steven" in to:
        return "steven"
    return default_user_id


def _extract_body(msg: dict) -> str:
    """Walk the message payload, preferring text/plain when usable.

    Some senders (Citi transaction alerts especially) put the real
    content in the HTML body and leave text/plain as boilerplate that
    says "click here to view your message". When text/plain has fewer
    than 2 `$` signs but HTML has at least one, we strip the HTML and
    return that instead.

    Backward-compatible: Amazon / Venmo / retailer parsers all see their
    usual text/plain bodies because those are dollar-rich.
    """
    def walk(part):
        body = part.get("body", {})
        if "data" in body:
            yield part.get("mimeType", ""), base64.urlsafe_b64decode(
                body["data"]
            ).decode("utf-8", errors="replace")
        for sub in part.get("parts", []) or []:
            yield from walk(sub)

    bodies = list(walk(msg["payload"]))
    text = next((b for m, b in bodies if m == "text/plain"), None)
    html = next((b for m, b in bodies if "html" in m), None)

    # Conservative: switch to stripped HTML only when text/plain has ZERO
    # dollar amounts (Citi alerts being the canonical case). Earlier this
    # used "< 2" which broke Amazon order confirmations whose text/plain
    # had a single $ and lots of items as "* " bullets — the bullets
    # don't survive HTML stripping. amazon.py also accepts raw HTML, so
    # if a parser really needs HTML it can request it explicitly.
    if html and "$" not in (text or "") and "$" in html:
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            for s in soup(["script", "style"]):
                s.decompose()
            return soup.get_text(separator="\n", strip=True)
        except Exception:  # noqa: BLE001 - fall back to text on any error
            pass

    return text or html or ""


def _load_parser(name: str):
    mod = importlib.import_module(f"bot.parsers.{name}")
    return mod.parse


def poll_once(settings: Settings) -> int:
    """Run one polling pass across all (account x source) pairs. Returns new-row count."""
    storage.init_db(settings.paths.database)
    new_count = 0

    # Phase 3.2 hardening: feed the LLM only spending-eligible categories
    # (excludes Credit Card Payments + scheduled-bill goals). Falls back to
    # YnabClient.list_categories() when the local ledger is empty (no history
    # imported yet).
    spending_cats = storage.list_categories_for_spending(settings.paths.database)
    if spending_cats:
        categories = [
            {"id": c["id"], "name": c["name"], "group": c["group_name"]}
            for c in spending_cats
        ]
    else:
        ynab = YnabClient(settings.ynab_token, settings.ynab.budget_id)
        categories = ynab.list_categories() if settings.ynab_token else []
    cat_engine = Categorizer(settings.ollama.endpoint, settings.ollama.model,
                              settings.ollama.temperature)

    for account in settings.gmail_accounts:
        # Choose IMAP when an App Password env var is configured and set;
        # otherwise fall back to the legacy OAuth path.
        imap_pw = _account_imap_password(account)
        use_imap = bool(imap_pw)
        # An account configured ONLY for Telegram routing (no IMAP password
        # and no OAuth token) has no Gmail integration — skip the email
        # poll so we don't log an OAuth error every 60 seconds. This is
        # the Allison case: she /starts the bot for DMs but the bot has
        # no Gmail credentials for her.
        if not use_imap and not (account.token_path or "").strip():
            log.debug("skipping gmail poll for %s (no IMAP/OAuth configured)",
                      account.email)
            continue
        if use_imap:
            from bot.gmail_imap import (
                GmailIMAP, extract_body as imap_extract_body,
                extract_headers as imap_extract_headers,
                message_id_for_dedupe as imap_msg_id,
            )

        for source in settings.email_sources:
            log.info("polling %s for %s (backend=%s)", account.email,
                     source.name, "imap" if use_imap else "oauth")

            # Backend-specific iteration. Each yields (email_id, body, headers).
            if use_imap:
                try:
                    msg_iter = _iter_imap(
                        account.email, imap_pw, source.query,
                        GmailIMAP, imap_extract_body, imap_extract_headers,
                        imap_msg_id,
                    )
                except Exception as e:  # noqa: BLE001
                    log.error("imap failed for %s: %s", account.email, e)
                    continue
            else:
                try:
                    svc = _build_gmail_service(account.token_path)
                    msg_iter = _iter_oauth(svc, source.query)
                except Exception as e:  # noqa: BLE001
                    log.error("oauth failed for %s: %s", account.email, e)
                    continue

            for email_id, body, headers, _mark_done in msg_iter:
                parser = _load_parser(source.parser)
                parsed = parser(
                    body,
                    subject=headers.get("Subject", ""),
                    date_header=headers.get("Date", ""),
                )
                if parsed["parse_status"] not in {"ok", "partial"}:
                    # Leave failed-parse emails in inbox so the user can
                    # see them and decide what to do.
                    continue
                # The "m" reference below uses email_id where the OAuth
                # path used to use m["id"]; both are stable per-message
                # ids the storage layer dedupes on.
                m = {"id": email_id}

                # CC/bank transaction alerts go straight to the ledger
                # (with dedupe). They aren't "orders to match" — they
                # represent the actual charge that hit the account.
                # Amazon/Venmo/retailer order confirmations keep using
                # the legacy pending_order flow because they need to be
                # matched against the eventual CC charge.
                if source.parser in {
                    "citi_alert", "chase_alert",
                    "coastal_transaction_alert",
                    "coastal_check_cleared",
                    "paypal_payment",
                }:
                    from bot import ingest
                    try:
                        ingest.ingest_signal(
                            settings.paths.database,
                            signal_kind=source.parser,
                            email_id=m["id"],
                            parsed=parsed,
                            user_id=account.user_id,
                            settings=settings,
                        )
                        new_count += 1
                        _mark_done()
                    except Exception as e:  # noqa: BLE001
                        log.warning("ingest %s failed for %s: %s",
                                    source.parser, m["id"], e)
                    continue

                # Balance summary signals (Coastal + Chase CC) write
                # account_balance_observed rows via ingest, no
                # ledger_txn / pending_order.
                if source.parser in {
                    "coastal_balance_summary",
                    "chase_balance_summary",
                    "citi_balance_summary",
                }:
                    from bot import ingest
                    try:
                        ingest.ingest_signal(
                            settings.paths.database,
                            signal_kind=source.parser,
                            email_id=m["id"],
                            parsed=parsed,
                            user_id=account.user_id,
                            settings=settings,
                        )
                        new_count += 1
                        _mark_done()
                    except Exception as e:  # noqa: BLE001
                        log.warning("balance ingest failed: %s", e)
                    continue

                # Legacy path: Amazon / Venmo / retailer_order →
                # pending_order, categorized for the Telegram UX.
                try:
                    row_id = storage.insert_pending_order(
                        settings.paths.database,
                        user_id=account.user_id,
                        source=parsed["source"],
                        external_id=parsed.get("order_id") or parsed.get("counterparty"),
                        email_id=m["id"],
                        order_date=parsed.get("order_date") or date.today(),
                        total_cents=parsed.get("total_cents") or parsed.get("amount_cents") or 0,
                        raw_summary=parsed.get("summary", ""),
                        raw_payload=parsed,
                        assigned_to_user_id=_person_from_recipient(
                            headers, account.user_id),
                    )
                    new_count += 1
                except sqlite3.IntegrityError:
                    continue

                # Amazon order emails often arrive HOURS after their charge
                # was already ingested and auto-bucketed to Unassigned. Sweep
                # backward so the late-arriving recipient re-buckets the
                # charge (redesign-v2 Phase 3; conservative single-match).
                if parsed.get("source") == "amazon" and row_id:
                    from bot import ingest
                    try:
                        ingest.retro_bucket_amazon_order(
                            settings.paths.database, order_id=row_id,
                        )
                    except Exception as e:  # noqa: BLE001
                        log.warning("amazon retro-bucket failed: %s", e)

                # Multi-order confirmation: Amazon packs one checkout into
                # several order numbers and sends a single "Ordered:" email.
                # Each extra order needs its own pending_order row, keyed by
                # a synthesized email_id — insert_pending_order is idempotent
                # on email_id alone, so reusing the parent's would silently
                # collapse them into the first order. That is how a real
                # $278.82 order vanished on 2026-07-04, leaving a card charge
                # nothing could match.
                for extra in (parsed.get("additional_orders") or []):
                    extra_order_id = extra.get("order_id")
                    if not extra_order_id:
                        continue
                    try:
                        extra_row_id = storage.insert_pending_order(
                            settings.paths.database,
                            user_id=account.user_id,
                            source=parsed["source"],
                            external_id=extra_order_id,
                            email_id=f"{m['id']}#{extra_order_id}",
                            order_date=parsed.get("order_date") or date.today(),
                            total_cents=extra.get("total_cents") or 0,
                            raw_summary=extra.get("summary", ""),
                            raw_payload=extra,
                            assigned_to_user_id=_person_from_recipient(
                                headers, account.user_id),
                        )
                        new_count += 1
                    except sqlite3.IntegrityError:
                        continue
                    if extra_row_id:
                        from bot import ingest
                        try:
                            ingest.retro_bucket_amazon_order(
                                settings.paths.database, order_id=extra_row_id,
                            )
                        except Exception as e:  # noqa: BLE001
                            log.warning("amazon retro-bucket (extra) failed: %s", e)

                # Venmo INFLOWS ("Jane paid you $20", charged_by) should
                # never run through the LLM — they're not spending. The
                # LLM was guessing Dining/Groceries because every option
                # in the spending-only category list is a spending
                # category. Short-circuit to Inflow: Ready to Assign so
                # YNAB's normal income flow handles it.
                if (parsed.get("source") == "venmo"
                        and parsed.get("direction") in {"received", "charged_by"}):
                    inflow_cat_id = _INFLOW_READY_TO_ASSIGN_ID
                    with storage.connect(settings.paths.database) as con:
                        con.execute(
                            "UPDATE pending_order SET suggested_category = ?, "
                            "suggested_confidence = 1.0 WHERE id = ?",
                            (inflow_cat_id, row_id),
                        )
                    storage.audit(settings.paths.database, "pending_order_inserted",
                                  {"id": row_id, "source": parsed["source"],
                                   "auto_inflow": True})
                    _mark_done()
                    continue

                payee_for_priors = parsed.get("counterparty") or parsed["source"]
                priors = storage.get_category_priors_for_payee(
                    settings.paths.database, payee_for_priors, top_n=5,
                )
                suggestion = cat_engine.suggest(
                    summary=parsed.get("summary", ""),
                    amount_cents=parsed.get("total_cents") or parsed.get("amount_cents") or 0,
                    date_str=str(parsed.get("order_date") or ""),
                    source=parsed["source"],
                    categories=categories,
                    priors=priors,
                )
                with storage.connect(settings.paths.database) as con:
                    con.execute(
                        "UPDATE pending_order SET suggested_category = ?, "
                        "suggested_confidence = ? WHERE id = ?",
                        (suggestion["category_id"], suggestion["confidence"], row_id),
                    )
                storage.audit(settings.paths.database, "pending_order_inserted",
                              {"id": row_id, "source": parsed["source"]})
                _mark_done()

    return new_count


def _iter_oauth(svc, query: str):
    """Yields (email_id, body, headers, mark_done) tuples from Gmail OAuth.

    OAuth path is a fallback; mark_done is a no-op (the OAuth code never
    learned the label-add behavior).
    """
    try:
        resp = svc.users().messages().list(
            userId="me", q=query, maxResults=50,
        ).execute()
    except Exception as e:  # noqa: BLE001
        log.error("gmail list failed: %s", e)
        return
    for m in resp.get("messages", []):
        msg = svc.users().messages().get(
            userId="me", id=m["id"], format="full",
        ).execute()
        body = _extract_body(msg)
        headers = {h["name"]: h["value"]
                   for h in msg["payload"].get("headers", [])}
        yield m["id"], body, headers, lambda *_a, **_kw: False


def _iter_imap(address: str, app_password: str, query: str,
               GmailIMAP_cls, body_fn, headers_fn, msgid_fn,
               on_processed=None):
    """Yields (email_id, body, headers) from a Gmail IMAP connection.

    Uses X-GM-RAW so the existing Gmail-syntax queries in config.yaml
    (e.g. `from:info6.citi.com newer_than:3d`) work unchanged.

    If ``on_processed`` is provided, it's called as
    ``on_processed(imap, uid)`` after the caller decides ingest
    succeeded. We thread the imap connection + uid through closures so
    the caller can call mark_processed() inside its for-loop.
    """
    with GmailIMAP_cls(address, app_password) as imap:
        uids = imap.search(query)
        log.info("imap %s: %d hits for %r", address, len(uids), query[:60])
        for uid in uids:
            msg = imap.fetch_message(uid)
            if msg is None:
                continue
            email_id = msgid_fn(msg, uid)
            body = body_fn(msg)
            headers = headers_fn(msg)
            # Closure that lets the caller mark this specific uid as done
            # without needing direct access to the imap connection.
            def _mark_done(label: str = "ynab-bot/processed",
                           _imap=imap, _uid=uid) -> bool:
                return _imap.mark_processed(_uid, label=label)
            yield email_id, body, headers, _mark_done


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    from bot.config import load_settings
    settings = load_settings()
    n = poll_once(settings)
    log.info("poll_once: %d new pending_orders", n)
