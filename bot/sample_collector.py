"""Phase 0 sample collector — captures raw email bodies from unparsed senders.

Mirrors `bot.gmail_watcher.poll_once` but does NOT parse, categorize, or notify.
For each `(gmail_account x observed_source)` pair, fetches matching messages
and stuffs the full plain-text + HTML body into `raw_email_sample` for offline
review. When you've seen enough samples to write a parser, move the entry from
`observed_sources` to `email_sources` and the real parser kicks in.

Idempotent on Gmail's message id, so it's safe to run on any cadence.
"""
from __future__ import annotations

import base64
import logging
from collections import Counter

from bot import storage
from bot.config import Settings
from bot.gmail_watcher import _build_gmail_service

log = logging.getLogger(__name__)


def _walk_payload(part):
    """Yield (mimeType, decoded_str) for every leaf part."""
    body = part.get("body", {})
    if "data" in body:
        try:
            decoded = base64.urlsafe_b64decode(body["data"]).decode(
                "utf-8", errors="replace",
            )
            yield part.get("mimeType", ""), decoded
        except Exception as e:  # noqa: BLE001 - keep collecting siblings
            log.warning("body decode failed: %s", e)
    for sub in part.get("parts", []) or []:
        yield from _walk_payload(sub)


def _split_body(msg: dict) -> tuple[str | None, str | None, bool]:
    """Returns (plaintext, html, has_attachments)."""
    bodies = list(_walk_payload(msg["payload"]))
    text = next((b for m, b in bodies if m == "text/plain"), None)
    html = next((b for m, b in bodies if "html" in m), None)
    has_attachments = any(
        (p.get("filename") or "")
        for p in (msg["payload"].get("parts") or [])
    )
    return text, html, has_attachments


def _sender_from_headers(headers: dict) -> str:
    """Normalize the 'From' header to the bare email address when possible."""
    raw = headers.get("From", "")
    # Common shape: "Name <addr@host>"; sometimes just "addr@host".
    if "<" in raw and ">" in raw:
        return raw.split("<", 1)[1].rsplit(">", 1)[0].strip().lower()
    return raw.strip().lower()


def collect_once(settings: Settings) -> dict:
    """Run one pass across all (account x observed_source) pairs.

    Returns {label: new_count} of how many fresh samples landed per label.
    """
    storage.init_db(settings.paths.database)
    per_label: Counter[str] = Counter()
    per_label_existing: Counter[str] = Counter()

    if not settings.observed_sources:
        log.info("no observed_sources configured — nothing to collect")
        return {}

    for account in settings.gmail_accounts:
        try:
            svc = _build_gmail_service(account.token_path)
        except Exception as e:  # noqa: BLE001
            log.error("gmail auth failed for %s: %s", account.email, e)
            continue

        for source in settings.observed_sources:
            try:
                resp = svc.users().messages().list(
                    userId="me", q=source.query, maxResults=50,
                ).execute()
            except Exception as e:  # noqa: BLE001
                log.error("gmail list failed for %s: %s", source.label, e)
                continue
            msgs = resp.get("messages", [])
            log.info("  %s: %d candidates", source.label, len(msgs))

            for m in msgs:
                try:
                    msg = svc.users().messages().get(
                        userId="me", id=m["id"], format="full",
                    ).execute()
                except Exception as e:  # noqa: BLE001
                    log.warning("gmail get %s failed: %s", m["id"], e)
                    continue

                headers = {
                    h["name"]: h["value"]
                    for h in msg["payload"].get("headers", [])
                }
                body_text, body_html, has_attachments = _split_body(msg)
                row_id = storage.insert_raw_email_sample(
                    settings.paths.database,
                    email_id=m["id"],
                    account_email=account.email,
                    sender=_sender_from_headers(headers),
                    sender_label=source.label,
                    subject=headers.get("Subject"),
                    date_header=headers.get("Date"),
                    internal_date=str(msg.get("internalDate") or ""),
                    snippet=msg.get("snippet"),
                    body_text=body_text,
                    body_html=body_html,
                    has_attachments=has_attachments,
                )
                if row_id is None:
                    per_label_existing[source.label] += 1
                else:
                    per_label[source.label] += 1

    summary = dict(per_label)
    storage.audit(
        settings.paths.database,
        "sample_collector_run",
        {"new": summary, "already_filed": dict(per_label_existing)},
    )
    log.info("collected: new=%s, existing=%s",
             summary, dict(per_label_existing))
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    from bot.config import load_settings
    n = collect_once(load_settings())
    total_new = sum(n.values())
    log.info("DONE: %d new samples across %d senders", total_new, len(n))
