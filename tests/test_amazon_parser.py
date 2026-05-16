from datetime import date
from pathlib import Path
from bot.parsers import amazon

FIXTURES = Path(__file__).parent / "fixtures" / "amazon_emails"


def _load(name: str) -> str:
    """Load first matching .txt fixture by filename prefix.

    We parse from the plain-text MIME part — Amazon emails ship both
    HTML and plain text, and the plain text is dramatically easier
    to extract structured data from. See parsing-knowledge.md.
    """
    matches = list(FIXTURES.glob(f"{name}*.txt"))
    assert matches, f"No fixture found matching {name}*.txt"
    return matches[0].read_text(encoding="utf-8")


def test_extract_order_id_from_confirmation():
    body = _load("auto-confirm-amazon-com")
    order_id = amazon.extract_order_id(body)
    assert order_id is not None
    parts = order_id.split("-")
    assert len(parts) == 3
    assert len(parts[0]) == 3
    assert len(parts[1]) == 7
    assert len(parts[2]) == 7


def test_extract_total_cents():
    body = _load("auto-confirm-amazon-com")
    total = amazon.extract_total_cents(body)
    # Pin the exact value from the fixture so regex drift fails loud
    # rather than silently capturing a wrong-but-in-range amount.
    assert total == 3216, f"expected 3216 (=$32.16) from Grand Total, got {total}"


def test_parse_email_date_header():
    d = amazon.parse_email_date_header("Fri, 8 May 2026 00:13:15 +0000")
    assert d == date(2026, 5, 8)


def test_parse_email_date_header_returns_none_on_empty():
    assert amazon.parse_email_date_header("") is None
    assert amazon.parse_email_date_header("garbage") is None
