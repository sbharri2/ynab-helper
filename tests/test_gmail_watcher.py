from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import date

from bot.config import Settings, GmailAccount, EmailSource, YnabConfig, OllamaConfig, TelegramConfig, Paths
from bot.gmail_watcher import poll_once
from bot import storage


def _settings(tmp_path):
    return Settings(
        gmail_accounts=[GmailAccount(
            email="t@gmail.com", user_id="steven",
            token_path=str(tmp_path / "tok.json"), chat_id=1,
        )],
        email_sources=[EmailSource(name="amazon", query="from:auto-confirm@amazon.com", parser="amazon")],
        ynab=YnabConfig(budget_id="b"),
        ollama=OllamaConfig(),
        telegram=TelegramConfig(),
        paths=Paths(database=str(tmp_path / "test.db")),
    )


@patch("bot.gmail_watcher._build_gmail_service")
@patch("bot.gmail_watcher.Categorizer")
@patch("bot.gmail_watcher.YnabClient")
def test_poll_once_inserts_pending_order(mock_ynab_cls, mock_cat_cls, mock_build, tmp_path):
    db = tmp_path / "test.db"
    storage.init_db(db)

    svc = MagicMock()
    svc.users().messages().list().execute.return_value = {"messages": [{"id": "m-1"}]}
    svc.users().messages().get().execute.return_value = {
        "id": "m-1",
        "payload": {"parts": [{"mimeType": "text/html",
                                "body": {"data": _b64(_sample_amazon_html())}}],
                    "headers": []},
    }
    mock_build.return_value = svc

    mock_cat = MagicMock()
    mock_cat.suggest.return_value = {"category_id": "c1", "confidence": 0.9, "reasoning": "x"}
    mock_cat_cls.return_value = mock_cat

    mock_ynab = MagicMock()
    mock_ynab.list_categories.return_value = [{"id": "c1", "name": "Baby", "group": "Family"}]
    mock_ynab_cls.return_value = mock_ynab

    settings = _settings(tmp_path)
    new_count = poll_once(settings)

    assert new_count == 1
    rows = storage.get_pending_orders(db, status="pending")
    assert len(rows) == 1
    assert rows[0]["suggested_category"] == "c1"


def _b64(s: str) -> str:
    import base64
    return base64.urlsafe_b64encode(s.encode()).decode()


def _sample_amazon_html() -> str:
    return """
    <html><body>
    Order placed: May 12, 2026
    Order # 123-4567890-1234567
    <a href="/dp/B0ABC">Pampers Diapers Size 4</a>
    Order Total: $47.23
    </body></html>
    """
