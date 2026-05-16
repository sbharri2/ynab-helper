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


def test_extract_items_returns_nonempty_list():
    body = _load("auto-confirm-amazon-com")
    items = amazon.extract_items(body)
    assert isinstance(items, list)
    assert len(items) > 0
    for item in items:
        assert 5 <= len(item) <= 300  # bumped from 200 — modern Amazon titles run long
        assert isinstance(item, str)


def test_parse_returns_complete_dict():
    body = _load("auto-confirm-amazon-com")
    result = amazon.parse(body, date_header="Fri, 8 May 2026 00:13:15 +0000")

    assert result["parse_status"] in {"ok", "partial"}
    assert result["order_id"]
    assert result["total_cents"] > 0
    assert isinstance(result["order_date"], date)
    assert result["order_date"] == date(2026, 5, 8)
    assert len(result["items"]) > 0
    assert result["source"] == "amazon"


def test_parse_handles_garbage_input():
    result = amazon.parse("not a real amazon email")
    assert result["parse_status"] == "partial"
    assert result["order_id"] is None
    assert result["items"] == ["(could not parse items)"]


def test_parse_falls_back_to_today_when_no_date_header():
    body = _load("auto-confirm-amazon-com")
    result = amazon.parse(body)
    assert result["order_date"] == date.today()
