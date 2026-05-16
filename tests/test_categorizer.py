import json
from unittest.mock import patch, MagicMock
from bot.categorizer import Categorizer


@patch("bot.categorizer.httpx.Client")
def test_suggests_category_from_items(mock_client_cls):
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.post.return_value.json.return_value = {
        "message": {"content": json.dumps({
            "category_id": "cat-baby-supplies",
            "confidence": 0.92,
            "reasoning": "Items are clearly baby-related."
        })}
    }
    mock_client_cls.return_value = mock_client

    categories = [
        {"id": "cat-baby-supplies", "name": "Baby Supplies", "group": "Family"},
        {"id": "cat-groceries", "name": "Groceries", "group": "Food"},
    ]
    cat = Categorizer(endpoint="http://localhost:11434", model="qwen2.5:14b")
    result = cat.suggest(
        summary="3 items: Diapers Size 4, Wipes 800ct, Formula",
        amount_cents=4723,
        date_str="2026-05-12",
        source="amazon",
        categories=categories,
    )
    assert result["category_id"] == "cat-baby-supplies"
    assert result["confidence"] > 0.5


@patch("bot.categorizer.httpx.Client")
def test_handles_ollama_down_gracefully(mock_client_cls):
    mock_client_cls.side_effect = Exception("connection refused")

    cat = Categorizer(endpoint="http://localhost:11434", model="qwen2.5:14b")
    result = cat.suggest(
        summary="x", amount_cents=100, date_str="2026-05-12",
        source="amazon", categories=[],
    )
    assert result["category_id"] is None
    assert result["confidence"] == 0.0
    assert "error" in result
