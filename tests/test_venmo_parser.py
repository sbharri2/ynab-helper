from pathlib import Path
from datetime import date
from bot.parsers import venmo

FIXTURES = Path(__file__).parent / "fixtures" / "venmo_emails"

# The "paid-you" fixture was saved manually during plan adjustment because the
# auto-pulled venmo@venmo.com fixture turned out to be a monthly statement
# (not transactional). See parsing-knowledge.md "Empirical findings".
PAID_YOU_FIXTURE_PREFIX = "venmo-venmo-com-paid-you"


def _load(name: str) -> str:
    matches = list(FIXTURES.glob(f"{name}*.txt"))
    assert matches, f"No fixture for {name}*.txt"
    return matches[0].read_text(encoding="utf-8")


def test_parse_subject_received():
    out = venmo.parse_subject("Jane Doe paid you $31.00")
    assert out["direction"] == "received"
    assert out["counterparty"] == "Jane Doe"
    assert out["amount_cents"] == 3100


def test_parse_subject_outgoing():
    out = venmo.parse_subject("You paid Jennifer Gilbert $123.45")
    assert out["direction"] == "paid"
    assert out["counterparty"] == "Jennifer Gilbert"
    assert out["amount_cents"] == 12345


def test_extract_note_from_body():
    body = _load(PAID_YOU_FIXTURE_PREFIX)
    note = venmo.extract_note_from_body(body)
    assert note == "Calf-tan and/or Capped Ham"


def test_parse_returns_complete_dict():
    body = _load(PAID_YOU_FIXTURE_PREFIX)
    result = venmo.parse(
        body,
        subject="Jane Doe paid you $31.00",
        date_header="Fri, 8 May 2026 00:13:15 +0000",
    )
    assert result["source"] == "venmo"
    assert result["parse_status"] in {"ok", "partial"}
    assert result["amount_cents"] == 3100
    assert result["direction"] == "received"
    assert result["counterparty"] == "Jane Doe"
    assert result["note"] == "Calf-tan and/or Capped Ham"
    assert result["order_date"] == date(2026, 5, 8)


def test_parse_falls_back_to_body_when_no_subject():
    body = _load(PAID_YOU_FIXTURE_PREFIX)
    result = venmo.parse(body)
    # Body contains "Jane Doe paid you $31.00" — body-only path should still work
    assert result["amount_cents"] == 3100
    assert result["direction"] == "received"
