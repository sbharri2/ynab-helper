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
            kwargs = {"plan_id": self.budget_id}
            if since:
                kwargs["since_date"] = since
            resp = api.get_transactions(**kwargs)
            results = []
            for t in resp.data.transactions:
                if t.category_id is not None:
                    continue
                # Transfers between accounts (credit-card payoffs, bank moves)
                # aren't expenses - skip so the bot never asks the user to
                # categorize them.
                if t.transfer_account_id is not None:
                    continue
                results.append({
                    "ynab_txn_id": str(t.id),
                    "ynab_account_id": str(t.account_id),
                    "payee": t.payee_name or "",
                    "amount_cents": _milliunits_to_cents(t.amount),
                    "txn_date": t.var_date,
                    "memo": t.memo or "",
                })
            return results

    def set_category(self, ynab_txn_id: str, category_id: str) -> None:
        with self._api() as api_client:
            api = ynab.TransactionsApi(api_client)
            api.update_transaction(
                plan_id=self.budget_id,
                transaction_id=ynab_txn_id,
                data=ynab.PutTransactionWrapper(
                    transaction=ynab.ExistingTransaction(category_id=category_id)
                ),
            )

    def create_category(
        self, name: str, category_group_id: str,
        *, goal_target_cents: int | None = None,
    ) -> dict:
        """Create a new category under the given category group.

        ``goal_target_cents`` is optional — when set, YNAB attaches a
        monthly "Need" goal of that amount. Converted to milliunits at
        the API boundary.

        Returns the new category as ``{id, name, group_id}`` so the
        caller can write it into the local mirror.
        """
        kwargs: dict = {"name": name, "category_group_id": category_group_id}
        if goal_target_cents is not None:
            kwargs["goal_target"] = _cents_to_milliunits(int(goal_target_cents))
        with self._api() as api_client:
            api = ynab.CategoriesApi(api_client)
            resp = api.create_category(
                plan_id=self.budget_id,
                data=ynab.PostCategoryWrapper(
                    category=ynab.NewCategory(**kwargs),
                ),
            )
            cat = resp.data.category
            return {
                "id": str(cat.id),
                "name": cat.name,
                "group_id": str(cat.category_group_id),
            }

    def update_category(
        self,
        category_id: str,
        *,
        name: str | None = None,
        category_group_id: str | None = None,
        note: str | None = None,
        goal_target: int | None = None,
    ) -> dict:
        """Patch an existing category. Any kwarg left as None is unchanged.

        ``goal_target`` is in cents; we convert to milliunits for the API.
        Used by the agent's ``move_category_to_group`` tool when the
        user wants to relocate a freshly-created category.
        """
        kwargs: dict = {}
        if name is not None:
            kwargs["name"] = name
        if category_group_id is not None:
            kwargs["category_group_id"] = category_group_id
        if note is not None:
            kwargs["note"] = note
        if goal_target is not None:
            kwargs["goal_target"] = _cents_to_milliunits(int(goal_target))
        with self._api() as api_client:
            api = ynab.CategoriesApi(api_client)
            resp = api.update_category(
                plan_id=self.budget_id,
                category_id=category_id,
                data=ynab.PatchCategoryWrapper(
                    category=ynab.ExistingCategory(**kwargs),
                ),
            )
            cat = resp.data.category
            return {
                "id": str(cat.id),
                "name": cat.name,
                "group_id": str(cat.category_group_id),
            }

    def list_category_groups(self) -> list[dict]:
        """All non-hidden groups, used to fuzzy-match `group_name` for category create."""
        with self._api() as api_client:
            api = ynab.CategoriesApi(api_client)
            resp = api.get_categories(plan_id=self.budget_id)
            return [
                {"id": str(g.id), "name": g.name}
                for g in resp.data.category_groups
                if not g.hidden and not g.deleted
            ]

    def list_categories(self) -> list[dict]:
        """Returns flat list of categories with parent group name attached."""
        with self._api() as api_client:
            api = ynab.CategoriesApi(api_client)
            resp = api.get_categories(plan_id=self.budget_id)
            results = []
            for group in resp.data.category_groups:
                if group.hidden or group.deleted:
                    continue
                for c in group.categories:
                    if c.hidden or c.deleted:
                        continue
                    results.append({
                        "id": str(c.id),
                        "name": c.name,
                        "group": group.name,
                    })
            return results
