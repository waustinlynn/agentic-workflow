"""DynamoDB-backed ``RunState`` persistence shared across Phase 1 local and Phase 2 Lambda."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

import boto3

from shared.models import RunState


class RunStateStore(Protocol):
    """Persistence contract for per-(repo, issue_or_pr) workflow state."""

    def get(self, repo_full_name: str, issue_or_pr_number: int) -> RunState | None: ...

    def put(self, state: RunState) -> None: ...


class DynamoDBTableProtocol(Protocol):
    """Minimal subset of ``boto3.resource('dynamodb').Table`` for ``DynamoDBRunStateStore``."""

    def get_item(self, *, Key: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def put_item(self, *, Item: Mapping[str, Any]) -> Mapping[str, Any]: ...


class DynamoDBRunStateStore:
    """Stores ``RunState`` records in the ``agentic_run_state`` DynamoDB table."""

    def __init__(
        self,
        table_name: str,
        *,
        table: DynamoDBTableProtocol | None = None,
        region_name: str | None = None,
    ) -> None:
        self._table_name = table_name
        self._table = table
        self._region_name = region_name

    def _get_table(self) -> DynamoDBTableProtocol:
        if self._table is None:
            resource = boto3.resource("dynamodb", region_name=self._region_name)
            self._table = resource.Table(self._table_name)
        return self._table

    def get(self, repo_full_name: str, issue_or_pr_number: int) -> RunState | None:
        response = self._get_table().get_item(
            Key={
                "pk": f"REPO#{repo_full_name}",
                "sk": f"ITEM#{issue_or_pr_number}",
            }
        )
        raw_item = response.get("Item")
        if not isinstance(raw_item, Mapping):
            return None
        return RunState.from_dynamodb_item(dict(raw_item))

    def put(self, state: RunState) -> None:
        self._get_table().put_item(Item=state.to_dynamodb_item())


class InMemoryRunStateStore:
    """In-memory ``RunStateStore`` for unit tests and local dry runs."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, int], RunState] = {}

    def get(self, repo_full_name: str, issue_or_pr_number: int) -> RunState | None:
        return self._items.get((repo_full_name, issue_or_pr_number))

    def put(self, state: RunState) -> None:
        self._items[(state.repo_full_name, state.issue_or_pr_number)] = state.model_copy(deep=True)
