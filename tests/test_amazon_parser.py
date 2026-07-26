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


# --- Multi-order confirmation emails -------------------------------------
# Amazon groups one checkout into several order numbers and sends ONE
# "Ordered:" email listing each order in its own block:
#
#     Order # / <id> / <items> / Grand Total: / $<amount>   (repeated)
#
# The parser used to read only the first id + first total, silently
# dropping the rest. Observed live 2026-07-04: a $278.82 order rode along
# with a $139.41 one and never reached the ledger.

_TWO_ORDER_BODY = """Ordered: "Widget A..." and 2 more items
Your Orders
Thanks for your order!
Arriving July 8 - July 9
Steven - APEX, NC
Order #
112-4520723-6491412
View or edit order
Widget A Deluxe Edition
Quantity: 1
$
129
99
Grand Total:
$139.41
Arriving Monday
Steven - APEX, NC
Order #
112-0628151-8215415
View or edit order
Widget B Standard Bounce
Quantity: 1
$
129
99
Widget C Standard Bounce
Quantity: 1
$
129
99
Grand Total:
$278.82
"""


def test_multi_order_email_keeps_first_order_as_primary():
    """Backwards compatibility: the primary fields must not shift."""
    parsed = amazon.parse(_TWO_ORDER_BODY, subject='Ordered: "Widget A..."')
    assert parsed["order_id"] == "112-4520723-6491412"
    assert parsed["total_cents"] == 13941


def test_multi_order_email_returns_the_second_order():
    parsed = amazon.parse(_TWO_ORDER_BODY, subject='Ordered: "Widget A..."')
    extra = parsed.get("additional_orders") or []
    assert len(extra) == 1, f"expected 1 additional order, got {len(extra)}"
    assert extra[0]["order_id"] == "112-0628151-8215415"
    assert extra[0]["total_cents"] == 27882


def test_multi_order_extra_orders_carry_their_own_items():
    parsed = amazon.parse(_TWO_ORDER_BODY, subject='Ordered: "Widget A..."')
    extra = parsed["additional_orders"][0]
    joined = " ".join(extra["items"])
    assert "Widget B" in joined
    assert "Widget C" in joined
    # The first order's item must NOT leak into the second order's block
    assert "Widget A" not in joined
    assert extra["summary"]


def test_single_order_email_has_no_additional_orders():
    """A normal one-order email must be completely unchanged."""
    body = _load("auto-confirm-amazon-com")
    parsed = amazon.parse(body)
    assert parsed.get("additional_orders") == []
    assert parsed["total_cents"] == 3216


def test_repeated_order_id_does_not_create_a_phantom_order():
    """Tracking links repeat the order number; that is not a second order."""
    body = (
        "Order #\n112-1111111-2222222\nWidget\nQuantity: 1\n"
        "Grand Total:\n$10.00\n"
        "https://www.amazon.com/progress-tracker/package?orderId=112-1111111-2222222\n"
    )
    parsed = amazon.parse(body)
    assert parsed["order_id"] == "112-1111111-2222222"
    assert parsed["total_cents"] == 1000
    assert parsed.get("additional_orders") == []
