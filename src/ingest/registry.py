"""Repo Registry lookup mapping ``repo_full_name`` -> SQS FIFO ``MessageGroupId``."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from shared.models import ExecutionTarget, RuntimeKind


class RepoRegistryEntry(BaseModel):
    """Registered repository routing metadata stored in DynamoDB."""

    model_config = ConfigDict(extra="forbid")

    repo_full_name: str = Field(min_length=3)
    message_group_id: str = Field(min_length=1, max_length=128)
    execution_target: ExecutionTarget = ExecutionTarget.LOCAL
    runtime: RuntimeKind = RuntimeKind.BASE
    enabled: bool = True

    @field_validator("message_group_id")
    @classmethod
    def validate_message_group_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or any(ord(ch) < 33 or ord(ch) > 126 for ch in cleaned):
            raise ValueError(
                f"message_group_id must contain printable non-whitespace ASCII chars: {value!r}"
            )
        return cleaned


class RepoRegistry(Protocol):
    """Abstraction over the repository routing registry."""

    def get_repo(self, repo_full_name: str) -> RepoRegistryEntry | None: ...


class DynamoDBClientProtocol(Protocol):
    """Minimal subset of ``boto3.client('dynamodb')`` needed for registry lookup."""

    def get_item(self, *, TableName: str, Key: Mapping[str, Any]) -> Mapping[str, Any]: ...


def _parse_dynamodb_attr(attr: Mapping[str, Any]) -> Any:
    if "S" in attr:
        return str(attr["S"])
    if "BOOL" in attr:
        return bool(attr["BOOL"])
    if "N" in attr:
        return attr["N"]
    return None


class DynamoDBRepoRegistry:
    """Looks up onboarded repositories in the ``agentic_repo_registry`` DynamoDB table."""

    def __init__(self, client: DynamoDBClientProtocol, table_name: str) -> None:
        self._client = client
        self._table_name = table_name

    def get_repo(self, repo_full_name: str) -> RepoRegistryEntry | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key={"repo_full_name": {"S": repo_full_name}},
        )
        raw_item = response.get("Item")
        if not isinstance(raw_item, Mapping):
            return None

        parsed = {key: _parse_dynamodb_attr(val) for key, val in raw_item.items()}
        return RepoRegistryEntry.model_validate(parsed)
