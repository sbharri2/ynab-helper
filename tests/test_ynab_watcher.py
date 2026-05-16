from datetime import date, datetime, time
from unittest.mock import patch, MagicMock
from bot import storage
from bot.config import Settings, GmailAccount, EmailSource, YnabConfig, OllamaConfig, TelegramConfig, Paths
from bot.ynab_watcher import poll_once


def _settings(tmp_path):
    return Settings(
        gmail_accounts=[GmailAccount(email="t@gmail.com", user_id="steven",
                                      token_path="x", chat_id=1)],
        email_sources=[EmailSource(name="amazon", query="x", parser="amazon")],
        ynab=YnabConfig(budget_id="b"),
        ollama=OllamaConfig(),
        telegram=TelegramConfig(),
        paths=Paths(database=str(tmp_path / "test.db")),
    )


@patch("bot.ynab_watcher.YnabClient")
def test_matches_categorized_order_to_amazon_charge(mock_cls, tmp_path):
    db = tmp_path / "test.db"
    storage.init_db(db)
    oid = storage.insert_pending_order(
        db, user_id="steven", source="amazon", external_id="123-4567890-1234567",
        email_id="m1", order_date=date(2026, 5, 12), total_cents=4723,
        raw_summary="x", raw_payload={},
    )
    storage.mark_order_categorized(db, oid, chosen_category="cat-baby")

    mock_y = MagicMock()
    mock_y.list_uncategorized.return_value = [{
        "ynab_txn_id": "tx-1", "ynab_account_id": "acc",
        "payee": "AMAZON.COM*ABC123", "amount_cents": -4723,
        "txn_date": date(2026, 5, 14), "memo": "",
    }]
    mock_y.list_categories.return_value = [{"id": "cat-baby", "name": "Baby", "group": "Family"}]
    mock_cls.return_value = mock_y

    poll_once(_settings(tmp_path))

    mock_y.set_category.assert_called_once_with("tx-1", "cat-baby")
    assert storage.list_unmatched_amazon_orders(db) == []


@patch("bot.ynab_watcher.YnabClient")
def test_enqueues_non_amazon_txn_for_digest(mock_cls, tmp_path):
    db = tmp_path / "test.db"
    storage.init_db(db)

    mock_y = MagicMock()
    mock_y.list_uncategorized.return_value = [{
        "ynab_txn_id": "tx-2", "ynab_account_id": "acc",
        "payee": "STARBUCKS #1234", "amount_cents": -1275,
        "txn_date": date(2026, 5, 14), "memo": "",
    }]
    mock_y.list_categories.return_value = []
    mock_cls.return_value = mock_y

    poll_once(_settings(tmp_path))

    with storage.connect(db) as con:
        rows = con.execute("SELECT * FROM pending_txn").fetchall()
        assert len(rows) == 1
        assert rows[0]["payee"] == "STARBUCKS #1234"
