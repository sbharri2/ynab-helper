from unittest.mock import MagicMock, patch
from datetime import date
from bot.ynab_client import YnabClient


@patch("bot.ynab_client.ynab")
def test_list_uncategorized_calls_api(mock_ynab):
    mock_api = MagicMock()
    mock_api.get_transactions.return_value.data.transactions = [
        MagicMock(
            id="tx-1", account_id="acc-1", payee_name="STARBUCKS",
            amount=-12750, date=date(2026, 5, 14), memo="",
            category_id=None,
        )
    ]
    mock_ynab.ApiClient.return_value.__enter__.return_value = MagicMock()
    mock_ynab.TransactionsApi.return_value = mock_api

    client = YnabClient(token="x", budget_id="b1")
    txns = client.list_uncategorized()

    assert len(txns) == 1
    assert txns[0]["ynab_txn_id"] == "tx-1"
    assert txns[0]["amount_cents"] == -1275  # YNAB milliunits → cents (÷10)


@patch("bot.ynab_client.ynab")
def test_set_category(mock_ynab):
    mock_api = MagicMock()
    mock_ynab.ApiClient.return_value.__enter__.return_value = MagicMock()
    mock_ynab.TransactionsApi.return_value = mock_api

    client = YnabClient(token="x", budget_id="b1")
    client.set_category("tx-1", "cat-uuid")

    mock_api.update_transaction.assert_called_once()
