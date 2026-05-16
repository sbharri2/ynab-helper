"""Thin wrapper around the official YNAB Python SDK.

Centralizes auth + the specific operations ynab-helper uses. All amounts
in milliunits are converted to cents (x0.1) for internal use; on write
we convert back (x10). YNAB stores outflows as negative.
"""
from __future__ import annotations

import logging
from datetime import date

import ynab

log = logging.getLogger(__name__)


def _milliunits_to_cents(m: int) -> int:
    return m // 10


def _cents_to_milliunits(c: int) -> int:
    return c * 10


class YnabClient:
    def __init__(self, token: str, budget_id: str):
        self.budget_id = budget_id
        self._config = ynab.Configuration(access_token=token)

    def _api(self):
        return ynab.ApiClient(self._config)

    def list_uncategorized(self, since: date | None = None) -> list[dict]:
        """Returns all uncategorized transactions, optionally since a date."""
        with self._api() as api_client:
            api = ynab.TransactionsApi(api_client)
            kwargs = {"budget_id": self.budget_id}
            if since:
                kwargs["since_date"] = since
            resp = api.get_transactions(**kwargs)
            results = []
            for t in resp.data.transactions:
                if t.category_id is not None:
                    continue
                results.append({
                    "ynab_txn_id": t.id,
                    "ynab_account_id": t.account_id,
                    "payee": t.payee_name or "",
                    "amount_cents": _milliunits_to_cents(t.amount),
                    "txn_date": t.date,
                    "memo": t.memo or "",
                })
            return results

    def set_category(self, ynab_txn_id: str, category_id: str) -> None:
        with self._api() as api_client:
            api = ynab.TransactionsApi(api_client)
            api.update_transaction(
                budget_id=self.budget_id,
                transaction_id=ynab_txn_id,
                data=ynab.PutTransactionWrapper(
                    transaction=ynab.ExistingTransaction(category_id=category_id)
                ),
            )

    def list_categories(self) -> list[dict]:
        """Returns flat list of categories with parent group name attached."""
        with self._api() as api_client:
            api = ynab.CategoriesApi(api_client)
            resp = api.get_categories(budget_id=self.budget_id)
            results = []
            for group in resp.data.category_groups:
                if group.hidden or group.deleted:
                    continue
                for c in group.categories:
                    if c.hidden or c.deleted:
                        continue
                    results.append({
                        "id": c.id,
                        "name": c.name,
                        "group": group.name,
                    })
            return results
