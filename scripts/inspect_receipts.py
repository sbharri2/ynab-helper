"""
Inspect recent Amazon and Venmo emails to validate the ynab-helper spec.

Run this AFTER `reauth_gmail.py` succeeds.

Outputs:
  - Per-sender counts and example subjects (last 60 days)
  - Pattern-presence checks for the assumptions in
    `docs/superpowers/specs/2026-05-16-parsing-knowledge.md`
  - One representative HTML body saved per sender to
    `tests/fixtures/{amazon|venmo}_emails/` for use as parser test fixtures
  - Markdown summary written to `receipt-inspection.md`

Run:
    python scripts/inspect_receipts.py
"""

import base64
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

sys.stdout.reconfigure(line_buffering=True)

REPO = Path(__file__).resolve().parent.parent
TOKEN_PATH = os.path.expanduser(
    r"~/.google_workspace_mcp/credentials/sbharri2@gmail.com.json"
)
FIXTURES_AMAZON = REPO / "tests" / "fixtures" / "amazon_emails"
FIXTURES_VENMO = REPO / "tests" / "fixtures" / "venmo_emails"
REPORT_PATH = REPO / "receipt-inspection.md"

# Patterns from parsing-knowledge.md
ORDER_ID_RE = re.compile(r"\d{3}-\d{7}-\d{7}")
TOTAL_RE = re.compile(r"Total[:\s]*\$?([\d,]+\.?\d{2})", re.I)
ORDER_PLACED_RE = re.compile(r"Order placed[:\s]+([A-Z][a-z]+ \d{1,2}(?:,? \d{4})?)", re.I)
VENMO_DIRECTIONS = ["You paid", "You charged", "paid you", "charged you"]


def get_service():
    creds = Credentials.from_authorized_user_file(TOKEN_PATH)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def list_messages(service, query, max_results=50):
    ids = []
    req = service.users().messages().list(userId="me", q=query, maxResults=100)
    while req is not None and len(ids) < max_results:
        resp = req.execute()
        ids.extend(m["id"] for m in resp.get("messages", []))
        if len(ids) >= max_results:
            break
        req = service.users().messages().list_next(req, resp)
    return ids[:max_results]


def get_message(service, msg_id):
    return service.users().messages().get(userId="me", id=msg_id, format="full").execute()


def headers_of(msg):
    h = {x["name"]: x["value"] for x in msg["payload"]["headers"]}
    return {
        "id": msg["id"],
        "from": h.get("From", ""),
        "subject": h.get("Subject", ""),
        "date": h.get("Date", ""),
    }


def extract_sender_email(raw):
    m = re.search(r"<([^>]+)>", raw)
    return (m.group(1) if m else raw).lower().strip()


def get_bodies(msg):
    def walk(part):
        out = []
        body = part.get("body", {})
        if "data" in body:
            try:
                decoded = base64.urlsafe_b64decode(body["data"]).decode("utf-8", errors="replace")
                out.append((part.get("mimeType", ""), decoded))
            except Exception:
                pass
        for sub in part.get("parts", []) or []:
            out.extend(walk(sub))
        return out

    return walk(msg["payload"])


def classify_amazon_sender(sender):
    rules = [
        ("auto-confirm", "Order confirmation (PRIMARY for MVP)"),
        ("shipment-tracking", "Shipment tracking (skip)"),
        ("order-update", "Order update / refund (maybe useful)"),
        ("digital-no-reply", "Digital purchase confirmation"),
        ("subscribe", "Subscribe & Save"),
        ("marketplace-messages", "3rd-party seller message (skip)"),
        ("return", "Return / refund"),
        ("payments-update", "Payment update"),
    ]
    for needle, label in rules:
        if needle in sender:
            return label
    return "Unknown / other"


def check_patterns(text, html, kind):
    """Return a dict of {pattern_name: bool} for the assumptions we want to verify."""
    if kind == "amazon":
        return {
            "order_id_present": bool(ORDER_ID_RE.search(text) or ORDER_ID_RE.search(html)),
            "total_phrase_present": bool(TOTAL_RE.search(text) or TOTAL_RE.search(html)),
            "order_placed_present": bool(ORDER_PLACED_RE.search(text) or ORDER_PLACED_RE.search(html)),
            "has_dp_link": "/dp/" in html or "/gp/product/" in html,
        }
    else:  # venmo
        return {
            "has_direction_phrase": any(p in text for p in VENMO_DIRECTIONS),
            "amount_present": bool(re.search(r"\$[\d,]+\.\d{2}", text)),
            "has_note": bool(re.search(r'"[^"]{2,100}"', text)),  # crude — quoted note
        }


def save_fixture(fixtures_dir, sender, msg_id, html, text):
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", sender.lower()).strip("-")
    base = fixtures_dir / f"{slug}_{msg_id[:8]}"
    if html:
        base.with_suffix(".html").write_text(html, encoding="utf-8")
    if text:
        base.with_suffix(".txt").write_text(text, encoding="utf-8")
    return base.name


def report_category(service, query, kind, fixtures_dir, report_lines):
    print(f"\n{'='*70}\n{kind.upper()}\n{'='*70}")
    print(f"Query: {query}\n")
    report_lines.append(f"\n## {kind.upper()}\n\nQuery: `{query}`\n")

    ids = list_messages(service, query, max_results=50)
    print(f"Found {len(ids)} messages in last 60 days\n")
    report_lines.append(f"Found **{len(ids)}** messages in last 60 days.\n")

    if not ids:
        report_lines.append("⚠️ No messages — adjust the query.\n")
        return

    # Pull full content for all (we need bodies for pattern checks anyway)
    msgs_by_sender = defaultdict(list)
    for mid in ids:
        try:
            msg = get_message(service, mid)
        except Exception as e:
            print(f"  skip {mid}: {e}")
            continue
        h = headers_of(msg)
        sender = extract_sender_email(h["from"])
        bodies = get_bodies(msg)
        html_body = next((b for m, b in bodies if "html" in m), "")
        text_body = next((b for m, b in bodies if m == "text/plain"), "")
        h["html"] = html_body
        h["text"] = text_body or re.sub(r"<[^>]+>", " ", html_body)
        msgs_by_sender[sender].append(h)

    report_lines.append(f"\n### Senders ({len(msgs_by_sender)} unique)\n")
    report_lines.append("| Sender | Count | Classified as |")
    report_lines.append("|---|---|---|")

    pattern_results_by_sender = {}

    for sender, msgs in sorted(msgs_by_sender.items(), key=lambda x: -len(x[1])):
        purpose = classify_amazon_sender(sender) if kind == "amazon" else "Venmo transactional"
        print(f"\n  {sender}  ({len(msgs)} emails)  →  {purpose}")
        report_lines.append(f"| `{sender}` | {len(msgs)} | {purpose} |")

        # Subject patterns (normalized — strip digits and names)
        subj_counter = Counter()
        for m in msgs:
            normalized = re.sub(r"\d", "#", m["subject"])
            normalized = re.sub(r"#{3,}", "###", normalized)
            subj_counter[normalized] += 1

        # Run pattern checks across all messages from this sender
        pattern_counts = defaultdict(int)
        for m in msgs:
            checks = check_patterns(m["text"], m["html"], kind)
            for name, ok in checks.items():
                if ok:
                    pattern_counts[name] += 1
        pattern_results_by_sender[sender] = (pattern_counts, len(msgs))

        # Save first message as a fixture
        if msgs:
            saved = save_fixture(fixtures_dir, sender, msgs[0]["id"], msgs[0]["html"], msgs[0]["text"])
            print(f"    fixture saved: {saved}")

    # Pattern verification table
    report_lines.append(f"\n### Pattern presence (per sender)\n")
    if kind == "amazon":
        report_lines.append("| Sender | order_id | total | order_placed | /dp/ link |")
        report_lines.append("|---|---|---|---|---|")
        for sender, (counts, total) in pattern_results_by_sender.items():
            report_lines.append(
                f"| `{sender}` | {counts.get('order_id_present',0)}/{total} | "
                f"{counts.get('total_phrase_present',0)}/{total} | "
                f"{counts.get('order_placed_present',0)}/{total} | "
                f"{counts.get('has_dp_link',0)}/{total} |"
            )
    else:
        report_lines.append("| Sender | direction phrase | amount | quoted note |")
        report_lines.append("|---|---|---|---|")
        for sender, (counts, total) in pattern_results_by_sender.items():
            report_lines.append(
                f"| `{sender}` | {counts.get('has_direction_phrase',0)}/{total} | "
                f"{counts.get('amount_present',0)}/{total} | "
                f"{counts.get('has_note',0)}/{total} |"
            )

    # Subject samples
    report_lines.append(f"\n### Example subjects (normalized — digits → #)\n")
    for sender, msgs in sorted(msgs_by_sender.items(), key=lambda x: -len(x[1])):
        report_lines.append(f"\n**`{sender}`**\n")
        subj_counter = Counter()
        for m in msgs:
            normalized = re.sub(r"\d", "#", m["subject"])
            normalized = re.sub(r"#{3,}", "###", normalized)
            subj_counter[normalized] += 1
        for subj, count in subj_counter.most_common(5):
            report_lines.append(f"- [{count}x] {subj[:100]}")


def main():
    service = get_service()
    prof = service.users().getProfile(userId="me").execute()
    print(f"Connected as: {prof['emailAddress']}")

    report_lines = [
        f"# Receipt Inspection Report",
        f"",
        f"Generated against `{prof['emailAddress']}` to validate the parsing assumptions in",
        f"`docs/superpowers/specs/2026-05-16-parsing-knowledge.md`.",
        f"",
        f"HTML fixtures saved under `tests/fixtures/` for parser unit-test seeding.",
    ]

    report_category(
        service,
        "from:amazon.com newer_than:60d",
        "amazon",
        FIXTURES_AMAZON,
        report_lines,
    )
    report_category(
        service,
        "from:venmo.com newer_than:60d",
        "venmo",
        FIXTURES_VENMO,
        report_lines,
    )

    REPORT_PATH.write_text("\n".join(report_lines), encoding="utf-8")
    print(f"\n{'='*70}\nReport written: {REPORT_PATH}\nFixtures in: {FIXTURES_AMAZON.parent}\n{'='*70}")


if __name__ == "__main__":
    main()
