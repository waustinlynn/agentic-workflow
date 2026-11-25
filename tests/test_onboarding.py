"""Unit tests for starter onboarding templates (#24) and `agentic onboard` CLI (#25)."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from shared.config import load_repo_config
from shared.github_auth import AppIdentity, GitHubAppCredentials
from shared.github_client import create_github_clients
from shared.labels import AGENT_LABEL_SPECS
from shared.models import ExecutionTarget, RuntimeKind
from worker.onboard import default_message_group_id, onboard_repository

TEMPLATES_ROOT = Path(__file__).resolve().parents[1] / "templates"


class _StaticCreds:
    def __init__(self, creds: dict[AppIdentity, GitHubAppCredentials]) -> None:
        self._creds = creds

    def get_credentials(self, identity: AppIdentity) -> GitHubAppCredentials:
        return self._creds[identity]


class _FakeDynamoDBPut:
    def __init__(self) -> None:
        self.items: list[tuple[str, Mapping[str, Any]]] = []

    def put_item(self, *, TableName: str, Item: Mapping[str, Any]) -> Mapping[str, Any]:
        self.items.append((TableName, Item))
        return {}


@pytest.fixture(scope="module")
def app_keys() -> dict[AppIdentity, GitHubAppCredentials]:
    out: dict[AppIdentity, GitHubAppCredentials] = {}
    for idx, identity in enumerate((AppIdentity.CODER, AppIdentity.REVIEWER), start=1):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")
        out[identity] = GitHubAppCredentials(
            identity=identity,
            app_id=str(idx * 100),
            private_key_pem=pem,
        )
    return out


class TestOnboardingTemplates:
    @pytest.mark.parametrize("stack", ["frontend", "backend", "mobile", "infra"])
    def test_starter_config_templates_pass_schema_validation(self, stack: str) -> None:
        cfg_path = TEMPLATES_ROOT / stack / ".agentic" / "config.yaml"
        assert cfg_path.is_file(), f"Missing template config: {cfg_path}"
        cfg = load_repo_config(cfg_path)
        assert cfg.stack.value == stack
        assert cfg.safety.allow_external_forks is False
        assert ".github/workflows/**" in cfg.safety.effective_protected_paths

    def test_shared_starter_files_exist(self) -> None:
        assert (TEMPLATES_ROOT / "AGENTS.md").is_file()
        assert (TEMPLATES_ROOT / "workflows" / "ci.yml").is_file()
        assert (TEMPLATES_ROOT / "workflows" / "deploy.yml").is_file()


class TestOnboardCli:
    def test_default_message_group_id_is_deterministic_and_sqs_safe(self) -> None:
        assert (
            default_message_group_id("waustinlynn/My_Cool.Repo") == "repo-waustinlynn-my_cool.repo"
        )

    def test_onboard_repository_all_checks_pass(
        self,
        app_keys: dict[AppIdentity, GitHubAppCredentials],
    ) -> None:
        cfg_yaml = (TEMPLATES_ROOT / "backend" / ".agentic" / "config.yaml").read_text(
            encoding="utf-8"
        )
        b64_cfg = base64.b64encode(cfg_yaml.encode("utf-8")).decode("ascii")
        created_labels: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            method = request.method
            if path.endswith("/installation"):
                token = request.headers["Authorization"].removeprefix("Bearer ")
                claims = jwt.decode(token, options={"verify_signature": False})
                return httpx.Response(200, json={"id": 10 if claims["iss"] == "100" else 20})
            if "/access_tokens" in path:
                return httpx.Response(
                    201,
                    json={"token": "ghs_ok", "expires_at": "2099-01-01T00:00:00Z"},
                )
            if method == "GET" and path == "/repos/waustinlynn/demo/labels":
                return httpx.Response(200, json=[])
            if method == "POST" and path == "/repos/waustinlynn/demo/labels":
                created_labels.append(json.loads(request.content)["name"])
                return httpx.Response(201, json={})
            if method == "GET" and path == "/repos/waustinlynn/demo/contents/.agentic/config.yaml":
                return httpx.Response(200, json={"encoding": "base64", "content": b64_cfg})
            if method == "GET" and path == "/repos/waustinlynn/demo":
                return httpx.Response(
                    200,
                    json={"default_branch": "main", "allow_auto_merge": True},
                )
            if method == "GET" and path == "/repos/waustinlynn/demo/branches/main/protection":
                return httpx.Response(
                    200,
                    json={
                        "required_pull_request_reviews": {"required_approving_review_count": 1},
                        "required_status_checks": {"contexts": ["verify"]},
                    },
                )
            return httpx.Response(404, text=f"Unhandled {method} {path}")

        http = httpx.Client(transport=httpx.MockTransport(handler))
        clients = create_github_clients(_StaticCreds(app_keys), http_client=http)
        ddb = _FakeDynamoDBPut()

        report = onboard_repository(
            "waustinlynn/demo",
            github_clients=clients,
            dynamodb_client=ddb,
            registry_table="agentic-workflow-prod-repo-registry",
            execution_target=ExecutionTarget.LOCAL,
            runtime=RuntimeKind.PYTHON,
        )

        assert report.all_passed is True
        assert len(created_labels) == len(AGENT_LABEL_SPECS)
        assert len(ddb.items) == 1
        table, item = ddb.items[0]
        assert table == "agentic-workflow-prod-repo-registry"
        assert item["message_group_id"] == {"S": "repo-waustinlynn-demo"}
        assert item["runtime"] == {"S": "python"}

    def test_onboard_reports_missing_config_and_branch_protection(
        self,
        app_keys: dict[AppIdentity, GitHubAppCredentials],
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            method = request.method
            if path.endswith("/installation"):
                return httpx.Response(200, json={"id": 10})
            if "/access_tokens" in path:
                return httpx.Response(
                    201,
                    json={"token": "ghs_ok", "expires_at": "2099-01-01T00:00:00Z"},
                )
            if method == "GET" and path == "/repos/waustinlynn/raw/labels":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "name": s.name.value,
                            "color": s.color,
                            "description": s.description,
                        }
                        for s in AGENT_LABEL_SPECS
                    ],
                )
            if method == "GET" and path == "/repos/waustinlynn/raw/contents/.agentic/config.yaml":
                return httpx.Response(404, json={"message": "Not Found"})
            if method == "GET" and path == "/repos/waustinlynn/raw":
                return httpx.Response(
                    200,
                    json={"default_branch": "main", "allow_auto_merge": False},
                )
            if method == "GET" and path == "/repos/waustinlynn/raw/branches/main/protection":
                return httpx.Response(404, json={"message": "Branch not protected"})
            return httpx.Response(404)

        http = httpx.Client(transport=httpx.MockTransport(handler))
        clients = create_github_clients(_StaticCreds(app_keys), http_client=http)
        ddb = _FakeDynamoDBPut()

        report = onboard_repository(
            "waustinlynn/raw",
            github_clients=clients,
            dynamodb_client=ddb,
            registry_table="agentic-workflow-prod-repo-registry",
        )

        assert report.all_passed is False
        by_name = {c.name: c.passed for c in report.checks}
        assert by_name["coder_app_installed"] is True
        assert by_name["reviewer_app_installed"] is True
        assert by_name["repo_registry_registered"] is True
        assert by_name["agent_labels_bootstrapped"] is True
        assert by_name["config_yaml_valid"] is False
        assert by_name["auto_merge_enabled"] is False
        assert by_name["branch_protection_configured"] is False
