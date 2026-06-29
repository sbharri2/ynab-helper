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

    def list_all_transactions(self, since: date | None = None) -> list[dict]:
        """Pull EVERY non-deleted YNAB transaction (categorized or not,
        including transfers). Used by the daily full-sync that mirrors
        YNAB into the local ledger so the bot doesn't go stale on
        auto-categorized accounts (Business Checking etc.).
        """
        with self._api() as api_client:
            api = ynab.TransactionsApi(api_client)
            kwargs = {"plan_id": self.budget_id}
            if since:
                kwargs["since_date"] = since
            resp = api.get_transactions(**kwargs)
            results: list[dict] = []
            for t in resp.data.transactions:
                if getattr(t, "deleted", False):
                    continue
                # Split transactions carry subtransactions, each with its
                # own category + a portion of the amount. We surface them
                # so the full-sync can mirror splits as child ledger rows
                # (otherwise the parent's category_id is null and the whole
                # charge looks uncategorized). Inherit the parent's payee
                # when a subtransaction doesn't override it.
                subs: list[dict] = []
                for st in (getattr(t, "subtransactions", None) or []):
                    if getattr(st, "deleted", False):
                        continue
                    subs.append({
                        "ynab_txn_id": str(st.id),
                        "ynab_category_id": (str(st.category_id)
                                              if st.category_id else None),
                        "transfer_account_id": (str(st.transfer_account_id)
                                                if st.transfer_account_id
                                                else None),
                        "transfer_transaction_id": (
                            str(st.transfer_transaction_id)
                            if getattr(st, "transfer_transaction_id", None)
                            else None
                        ),
                        "payee": st.payee_name or (t.payee_name or ""),
                        "amount_cents": _milliunits_to_cents(st.amount),
                        "memo": st.memo or "",
                    })
                results.append({
                    "ynab_txn_id": str(t.id),
                    "ynab_account_id": str(t.account_id),
                    "ynab_category_id": (str(t.category_id)
                                          if t.category_id else None),
                    "transfer_account_id": (str(t.transfer_account_id)
                                            if t.transfer_account_id else None),
                    "transfer_transaction_id": (
                        str(t.transfer_transaction_id)
                        if getattr(t, "transfer_transaction_id", None)
                        else None
                    ),
                    "payee": t.payee_name or "",
                    "amount_cents": _milliunits_to_cents(t.amount),
                    "txn_date": t.var_date,
                    "memo": t.memo or "",
                    "cleared": (t.cleared.value if t.cleared else "uncleared"),
                    "approved": bool(t.approved),
                    "subtransactions": subs,
                })
            return results

    def list_accounts(self) -> list[dict]:
        """Return all non-deleted accounts with current balances. Used by
        the daily full-sync to refresh ``account.balance_cents`` /
        ``cleared_balance_cents`` so the reconciler has a fresh anchor.
        """
        with self._api() as api_client:
            api = ynab.AccountsApi(api_client)
            resp = api.get_accounts(plan_id=self.budget_id)
            out: list[dict] = []
            for a in resp.data.accounts:
                if a.deleted or a.closed:
                    continue
                out.append({
                    "ynab_account_id": str(a.id),
                    "name": a.name,
                    "type": str(a.type.value if a.type else "checking"),
                    "balance_cents": _milliunits_to_cents(a.balance),
                    "cleared_balance_cents": _milliunits_to_cents(
                        a.cleared_balance
                    ),
                    "on_budget": bool(a.on_budget),
                })
            return out

    def set_category(self, ynab_txn_id: str, category_id: str,
                     approved: bool = True) -> None:
        """Assign a category to a YNAB transaction and mark it approved.

        YNAB transactions imported by the bank feed land as
        approved=false — they still need a human (or a third-party) to
        sign off. The bot's bot Telegram prompt IS that human signoff,
        so we set approved=True in the same PATCH. The user can flip
        ``approved`` back to False if they need a different workflow.
        """
        with self._api() as api_client:
            api = ynab.TransactionsApi(api_client)
            api.update_transaction(
                plan_id=self.budget_id,
                transaction_id=ynab_txn_id,
                data=ynab.PutTransactionWrapper(
                    transaction=ynab.ExistingTransaction(
                        category_id=category_id,
                        approved=approved,
                    )
                ),
            )

    def create_transaction(
        self, *, account_id: str, posted_date: date,
        amount_cents: int, payee_name: str,
        memo: str | None = None, category_id: str | None = None,
        cleared: str = "cleared", approved: bool = True,
        import_id: str | None = None,
    ) -> dict:
        """Create a new transaction in YNAB.

        Use case: backfilling transactions captured outside YNAB's Direct
        Import (e.g., parsed from a bank QFX export, or pushed from the
        bot's email-alert pipeline). Returns the created transaction dict
        with its YNAB id so the local ledger can stamp ynab_txn_id.

        ``import_id`` is YNAB's dedupe handle — pass any stable string
        (e.g., the QFX FITID, or our own dedupe_key) and YNAB will refuse
        to import the same id twice. Format must be ≤36 chars.
        """
        with self._api() as api_client:
            api = ynab.TransactionsApi(api_client)
            txn = ynab.NewTransaction(
                account_id=account_id,
                var_date=posted_date,
                amount=_cents_to_milliunits(amount_cents),
                payee_name=payee_name[:50] if payee_name else None,
                memo=memo[:200] if memo else None,
                category_id=category_id,
                cleared=cleared,
                approved=approved,
                import_id=import_id[:36] if import_id else None,
            )
            resp = api.create_transaction(
                plan_id=self.budget_id,
                data=ynab.PostTransactionsWrapper(transaction=txn),
            )
            t = resp.data.transaction
            return {
                "id": t.id,
                "date": str(t.var_date),
                "amount_cents": _milliunits_to_cents(t.amount),
                "payee_name": t.payee_name,
            }

    def delete_transaction(self, ynab_txn_id: str) -> None:
        """Soft-delete a transaction from YNAB. Used by the
        Tauri 'Sync to YNAB' / drift-cleanup flows when we detect a
        phantom row that exists in YNAB but doesn't match bank reality.

        Idempotent — deleting an already-deleted txn is a no-op.
        """
        with self._api() as api_client:
            api = ynab.TransactionsApi(api_client)
            api.delete_transaction(
                plan_id=self.budget_id,
                transaction_id=ynab_txn_id,
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

    def create_category_group(self, name: str) -> dict:
        """Create a new category group. Returns ``{id, name}``.

        Used when adding a category under a group that doesn't exist yet
        (e.g. the 'Personal Business' group for side-business tracking).
        """
        with self._api() as api_client:
            api = ynab.CategoriesApi(api_client)
            resp = api.create_category_group(
                plan_id=self.budget_id,
                data=ynab.PostCategoryGroupWrapper(
                    category_group=ynab.SaveCategoryGroup(name=name),
                ),
            )
            grp = resp.data.category_group
            return {"id": str(grp.id), "name": grp.name}

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
