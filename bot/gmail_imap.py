"""IMAP-based Gmail access (replaces the OAuth path).

Why: the OpenClaw OAuth app sits in Google's "Testing" status with
restricted scopes (`gmail.readonly`, `gmail.modify`), which means refresh
tokens die every 7 days. Manually re-running the OAuth flow weekly is
not acceptable. Google still supports IMAP for Gmail; using an App
Password (generated under Account Security → 2-Step Verification → App
passwords) means no token expiry until the user revokes it.

Tradeoffs vs the Gmail API:
  - We use the RFC 5322 Message-ID header for email_id (was Gmail's
    internal hex id). Old rows keyed by Gmail id are unaffected; new
    rows just use a different format.
  - Search uses Gmail's `X-GM-RAW` IMAP extension, so existing
    config queries like `from:info6.citi.com newer_than:3d` still
    work verbatim.
  - Label modification (not currently used by the bot) would need
    `X-GM-LABELS` STORE commands.

Public surface used by gmail_watcher.poll_once and sample_collector:

    with GmailIMAP(email, app_password) as imap:
        for uid in imap.search("from:info6.citi.com newer_than:3d"):
            msg = imap.fetch_message(uid)
            if msg is None: continue
            body = extract_body(msg)
            headers = extract_headers(msg)
            email_id = message_id_for_dedupe(msg, uid)
            ...
"""
from __future__ import annotations

import email
import imaplib
import logging
from email.message import Message
from typing import Iterator

log = logging.getLogger(__name__)

GMAIL_IMAP_HOST = "imap.gmail.com"
GMAIL_IMAP_PORT = 993


class GmailIMAP:
    """Context-managed IMAP4_SSL connection to Gmail.

    Opens the mailbox in READ-WRITE mode so callers can label processed
    messages and remove them from INBOX. The bot uses Gmail's
    X-GM-LABELS IMAP extension via STORE so the labels match what the
    user sees in Gmail's UI.
    """

    def __init__(self, address: str, app_password: str,
                 mailbox: str = '"[Gmail]/All Mail"') -> None:
        self.address = address
        self.app_password = app_password
        # Default to All Mail so we hit everything (matching Gmail API
        # default scope). Inbox-only callers can pass mailbox='INBOX'.
        self.mailbox = mailbox
        self._conn: imaplib.IMAP4_SSL | None = None

    def __enter__(self) -> "GmailIMAP":
        self._conn = imaplib.IMAP4_SSL(GMAIL_IMAP_HOST, GMAIL_IMAP_PORT)
        self._conn.login(self.address, self.app_password)
        # readonly=False so STORE works for mark_processed().
        typ, _ = self._conn.select(self.mailbox, readonly=False)
        if typ != "OK":
            raise RuntimeError(
                f"IMAP SELECT {self.mailbox} failed for {self.address}: {typ}"
            )
        return self

    def __exit__(self, *_exc) -> None:
        if self._conn is None:
            return
        try:
            self._conn.close()
        except imaplib.IMAP4.error:
            pass
        try:
            self._conn.logout()
        except imaplib.IMAP4.error:
            pass

    def search(self, gmail_query: str) -> list[bytes]:
        """Run a Gmail-syntax search via the X-GM-RAW IMAP extension.

        Returns a list of UID bytes. Empty list on no results or error.
        """
        assert self._conn is not None
        # X-GM-RAW takes a quoted string; embedded quotes in the user
        # query are escaped IMAP-style by doubling. In practice our
        # queries don't include literal quotes around tokens, but the
        # subject:("X" OR "Y") form does — they should be passed through
        # because Gmail interprets them.
        quoted = '"' + gmail_query.replace("\\", "\\\\").replace('"', '\\"') + '"'
        typ, data = self._conn.uid("SEARCH", "X-GM-RAW", quoted)
        if typ != "OK":
            log.warning("IMAP X-GM-RAW search failed: %s %s", typ, data)
            return []
        if not data or not data[0]:
            return []
        return data[0].split()

    def fetch_message(self, uid: bytes) -> Message | None:
        """Fetch the full RFC822 payload for a UID and parse to a Message."""
        assert self._conn is not None
        typ, data = self._conn.uid("FETCH", uid, "(RFC822)")
        if typ != "OK" or not data:
            return None
        for item in data:
            if isinstance(item, tuple) and len(item) >= 2:
                raw_bytes = item[1]
                if isinstance(raw_bytes, (bytes, bytearray)):
                    return email.message_from_bytes(bytes(raw_bytes))
        return None

    def mark_processed(
        self, uid: bytes, label: str = "ynab-bot/processed",
        remove_from_inbox: bool = True,
    ) -> bool:
        """Tag a message with a Gmail label and (optionally) archive it.

        Used by gmail_watcher after a successful ingest so processed
        emails don't keep cluttering the user's inbox. Failures stay
        in inbox so the user sees them.

        Returns True on success. Always safe to retry — labels and
        archive are idempotent.
        """
        assert self._conn is not None
        ok = True
        # Add the label. Quote the label so spaces / slashes are fine
        # (Gmail interprets `/` as nested labels in the UI).
        try:
            typ, _ = self._conn.uid(
                "STORE", uid, "+X-GM-LABELS", f'"{label}"',
            )
            if typ != "OK":
                log.warning("IMAP STORE +X-GM-LABELS %r failed: %s", label, typ)
                ok = False
        except Exception as e:  # noqa: BLE001
            log.warning("mark_processed label add failed: %s", e)
            ok = False

        # Archive by removing \Inbox label. Backslash-escape in the IMAP
        # payload so Gmail recognizes it as a system label.
        if remove_from_inbox:
            try:
                typ, _ = self._conn.uid(
                    "STORE", uid, "-X-GM-LABELS", '"\\\\Inbox"',
                )
                if typ != "OK":
                    log.warning("IMAP STORE -X-GM-LABELS Inbox failed: %s", typ)
                    ok = False
            except Exception as e:  # noqa: BLE001
                log.warning("mark_processed inbox remove failed: %s", e)
                ok = False
        return ok


def extract_body(msg: Message) -> str:
    """Walk the message; prefer text/plain; fall back to stripped HTML
    when text/plain has no `$` amounts but HTML does (Citi-alert case).
    Mirrors the heuristic in gmail_watcher._extract_body.
    """
    text_part = None
    html_part = None
    text_charset = None
    html_charset = None
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype == "text/plain" and text_part is None:
            text_part = part.get_payload(decode=True)
            text_charset = part.get_content_charset() or "utf-8"
        elif ctype == "text/html" and html_part is None:
            html_part = part.get_payload(decode=True)
            html_charset = part.get_content_charset() or "utf-8"

    def _decode(blob: bytes | None, charset: str | None) -> str | None:
        if blob is None:
            return None
        try:
            return blob.decode(charset or "utf-8", errors="replace")
        except (UnicodeDecodeError, LookupError):
            return blob.decode("utf-8", errors="replace")

    text = _decode(text_part, text_charset)
    html = _decode(html_part, html_charset)

    if html and "$" not in (text or "") and "$" in html:
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            for s in soup(["script", "style"]):
                s.decompose()
            return soup.get_text(separator="\n", strip=True)
        except Exception:  # noqa: BLE001
            pass

    return text or html or ""


def extract_headers(msg: Message) -> dict[str, str]:
    """Flatten message headers into a dict, decoding RFC 2047 encodings.

    Without this, headers like
        Subject: =?UTF-8?B?T3JkZXJlZDogIkFtYXpvbi4uLiI=?=
    arrive at downstream parsers as the encoded form, and pattern
    matches like "starts with 'Ordered:'" silently fail. Use
    email.header.decode_header + make_header so the dict has the
    human-readable string the parser expects.
    """
    from email.header import decode_header, make_header
    out: dict[str, str] = {}
    for k, v in msg.items():
        try:
            decoded = str(make_header(decode_header(str(v))))
        except Exception:  # noqa: BLE001 - fall back to raw on parse error
            decoded = str(v)
        out[str(k)] = decoded
    return out


def message_id_for_dedupe(msg: Message, fallback_uid: bytes) -> str:
    """Return a stable dedup key. Prefer the RFC 5322 Message-ID header;
    fall back to the IMAP UID if absent (some senders omit it).
    """
    mid = msg.get("Message-ID") or msg.get("Message-Id")
    if mid:
        return str(mid).strip().strip("<>")
    try:
        return fallback_uid.decode("ascii", errors="ignore")
    except AttributeError:
        return str(fallback_uid)
