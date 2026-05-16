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
def test_list_uncategorized_filters_out_categorized(mock_ynab):
    """The actual business rule — only category_id=None should come through."""
    mock_api = MagicMock()
    mock_api.get_transactions.return_value.data.transactions = [
        MagicMock(id="tx-uncat", account_id="a", payee_name="X", amount=-1000,
                  date=date(2026, 5, 1), memo="", category_id=None),
        MagicMock(id="tx-cat", account_id="a", payee_name="Y", amount=-2000,
                  date=date(2026, 5, 2), memo="", category_id="some-cat-id"),
    ]
    mock_ynab.ApiClient.return_value.__enter__.return_value = MagicMock()
    mock_ynab.TransactionsApi.return_value = mock_api

    txns = YnabClient(token="x", budget_id="b1").list_uncategorized()
    ids = [t["ynab_txn_id"] for t in txns]
    assert ids == ["tx-uncat"]


@patch("bot.ynab_client.ynab")
def test_list_uncategorized_coalesces_none_strings(mock_ynab):
    """payee_name=None and memo=None should become "" (not None) for downstream safety."""
    mock_api = MagicMock()
    mock_api.get_transactions.return_value.data.transactions = [
        MagicMock(id="tx-1", account_id="a", payee_name=None, amount=-500,
                  date=date(2026, 5, 1), memo=None, category_id=None),
    ]
    mock_ynab.ApiClient.return_value.__enter__.return_value = MagicMock()
    mock_ynab.TransactionsApi.return_value = mock_api

    [t] = YnabClient(token="x", budget_id="b1").list_uncategorized()
    assert t["payee"] == ""
    assert t["memo"] == ""


@patch("bot.ynab_client.ynab")
def test_set_category_sends_correct_envelope(mock_ynab):
    """Inspect the actual call kwargs — not just that update_transaction was called."""
    mock_api = MagicMock()
    mock_ynab.ApiClient.return_value.__enter__.return_value = MagicMock()
    mock_ynab.TransactionsApi.return_value = mock_api

    YnabClient(token="x", budget_id="budget-uuid").set_category("tx-1", "cat-uuid")

    call_kwargs = mock_api.update_transaction.call_args.kwargs
    assert call_kwargs["budget_id"] == "budget-uuid"
    assert call_kwargs["transaction_id"] == "tx-1"
    # The envelope is constructed as PutTransactionWrapper(transaction=ExistingTransaction(category_id=...))
    # Both classes are accessed through the mocked `ynab` module, so verify the chain:
    mock_ynab.PutTransactionWrapper.assert_called_once()
    mock_ynab.ExistingTransaction.assert_called_once_with(category_id="cat-uuid")


@patch("bot.ynab_client.ynab")
def test_list_categories_filters_hidden_and_deleted(mock_ynab):
    """Both group-level and category-level hidden/deleted flags must filter out items."""
    visible_cat = MagicMock(id="c-ok", name="Groceries", hidden=False, deleted=False)
    hidden_cat = MagicMock(id="c-hidden", name="Old", hidden=True, deleted=False)
    deleted_cat = MagicMock(id="c-del", name="Removed", hidden=False, deleted=True)

    visible_group = MagicMock(name="Food", hidden=False, deleted=False)
    visible_group.categories = [visible_cat, hidden_cat, deleted_cat]
    visible_group.name = "Food"  # MagicMock's `name` kwarg is a special attr; set it explicitly

    hidden_group = MagicMock(name="ShouldNotAppear", hidden=True, deleted=False)
    hidden_group.categories = [MagicMock(id="c-leak", name="Leak", hidden=False, deleted=False)]
    hidden_group.name = "ShouldNotAppear"

    mock_api = MagicMock()
    mock_api.get_categories.return_value.data.category_groups = [visible_group, hidden_group]
    mock_ynab.ApiClient.return_value.__enter__.return_value = MagicMock()
    mock_ynab.CategoriesApi.return_value = mock_api

    cats = YnabClient(token="x", budget_id="b1").list_categories()
    ids = [c["id"] for c in cats]
    assert ids == ["c-ok"]
    assert cats[0]["group"] == "Food"


@patch("bot.ynab_client.ynab")
def test_set_category(mock_ynab):
    mock_api = MagicMock()
    mock_ynab.ApiClient.return_value.__enter__.return_value = MagicMock()
    mock_ynab.TransactionsApi.return_value = mock_api

    client = YnabClient(token="x", budget_id="b1")
    client.set_category("tx-1", "cat-uuid")

    mock_api.update_transaction.assert_called_once()
