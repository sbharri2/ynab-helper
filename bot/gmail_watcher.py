"""Multi-source Gmail poller.

For each (gmail_account x email_source) pair:
  1. Search Gmail for matching new messages
  2. Parse each with the source-specific parser
  3. Insert into pending_order (idempotent on email_id)
  4. Ask the categorizer for a suggestion
  5. Persist suggestion on the row

The Telegram bot independently watches for new pending_order rows and pushes
them to the user - this module does not talk to Telegram directly.
"""
from __future__ import annotations

import base64
import importlib
import logging
import os
from datetime import date

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import sqlite3

from bot import storage
from bot.config import Settings
from bot.categorizer import Categorizer
from bot.ynab_client import YnabClient

log = logging.getLogger(__name__)


def _build_gmail_service(token_path: str):
    token_path = os.path.expanduser(token_path)
    creds = Credentials.from_authorized_user_file(token_path)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(token_path, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _extract_body(msg: dict) -> str:
    """Walk the message payload, preferring text/plain over text/html."""
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
    return text or html or ""  # prefer text/plain


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
        svc = _build_gmail_service(account.token_path)
        for source in settings.email_sources:
            log.info("polling %s for %s", account.email, source.name)
            try:
                resp = svc.users().messages().list(
                    userId="me", q=source.query, maxResults=50
                ).execute()
            except Exception as e:
                log.error("gmail list failed: %s", e)
                continue
            for m in resp.get("messages", []):
                msg = svc.users().messages().get(userId="me", id=m["id"], format="full").execute()
                body = _extract_body(msg)
                headers = {h["name"]: h["value"]
                           for h in msg["payload"].get("headers", [])}
                parser = _load_parser(source.parser)
                parsed = parser(
                    body,
                    subject=headers.get("Subject", ""),
                    date_header=headers.get("Date", ""),
                )
                if parsed["parse_status"] not in {"ok", "partial"}:
                    continue
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
                    )
                    new_count += 1
                except sqlite3.IntegrityError:
                    continue

                # Categorize. Phase 3.2: inject historical priors for the payee
                # so the LLM strongly favors how Steven actually categorizes this
                # merchant (e.g. Amazon → Groceries/Household, not "Chase Amazon").
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

    return new_count


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    from bot.config import load_settings
    settings = load_settings()
    n = poll_once(settings)
    log.info("poll_once: %d new pending_orders", n)
