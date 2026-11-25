"""Unit tests for 2-App GitHub authentication, API operations, and label bootstrapping."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from shared.github_auth import (
    AppIdentity,
    EnvCredentialProvider,
    GitHubAppAuth,
    GitHubAppCredentials,
    GitHubAuthError,
    SecretsManagerCredentialProvider,
)
from shared.github_client import (
    GitHubAPIError,
    GitHubClient,
    create_github_clients,
)
from shared.labels import AGENT_LABEL_SPECS, AgentLabel
from shared.models import (
    MergeMethod,
    ReviewComment,
    ReviewCommentSeverity,
    ReviewResult,
    ReviewVerdict,
)


def _generate_rsa_keypair() -> tuple[str, str]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )
    return private_pem, public_pem


class _StaticCredentialProvider:
    def __init__(self, creds: dict[AppIdentity, GitHubAppCredentials]) -> None:
        self._creds = creds

    def get_credentials(self, identity: AppIdentity) -> GitHubAppCredentials:
        return self._creds[identity]


class _FakeSecretsManager:
    def __init__(self, secrets: dict[str, str]) -> None:
        self._secrets = secrets
        self.calls: list[str] = []

    def get_secret_value(self, *, SecretId: str) -> Mapping[str, Any]:
        self.calls.append(SecretId)
        return {"SecretString": self._secrets[SecretId]}


@pytest.fixture(scope="module")
def rsa_keys() -> dict[str, tuple[str, str]]:
    return {
        "coder": _generate_rsa_keypair(),
        "reviewer": _generate_rsa_keypair(),
    }


class TestCredentialProviders:
    def test_env_provider_reads_inline_escaped_and_file_keys(
        self,
        tmp_path: Path,
        rsa_keys: dict[str, tuple[str, str]],
    ) -> None:
        coder_pem, _ = rsa_keys["coder"]
        reviewer_pem, _ = rsa_keys["reviewer"]
        reviewer_file = tmp_path / "reviewer.pem"
        reviewer_file.write_text(reviewer_pem, encoding="utf-8")

        env = {
            "GITHUB_CODER_APP_ID": "1001",
            "GITHUB_CODER_PRIVATE_KEY": coder_pem.replace("\n", "\\n"),
            "GITHUB_REVIEWER_APP_ID": "2002",
            "GITHUB_REVIEWER_PRIVATE_KEY_PATH": str(reviewer_file),
        }
        provider = EnvCredentialProvider(env)

        coder = provider.get_credentials(AppIdentity.CODER)
        reviewer = provider.get_credentials(AppIdentity.REVIEWER)
        assert coder.app_id == "1001"
        assert "BEGIN PRIVATE KEY" in coder.private_key_pem
        assert "\n" in coder.private_key_pem
        assert reviewer.app_id == "2002"
        assert reviewer.private_key_pem == reviewer_pem.strip()

    def test_env_provider_raises_when_missing(self) -> None:
        provider = EnvCredentialProvider({})
        with pytest.raises(GitHubAuthError, match="GITHUB_CODER_APP_ID"):
            provider.get_credentials(AppIdentity.CODER)

    def test_secrets_manager_provider_caches_and_supports_both_identities(
        self,
        rsa_keys: dict[str, tuple[str, str]],
    ) -> None:
        coder_pem, _ = rsa_keys["coder"]
        reviewer_pem, _ = rsa_keys["reviewer"]
        fake_sm = _FakeSecretsManager(
            {
                "agentic/coder": json.dumps({"app_id": "111", "private_key": coder_pem}),
                "agentic/reviewer": json.dumps({"app_id": "222", "private_key": reviewer_pem}),
            }
        )
        provider = SecretsManagerCredentialProvider(
            {
                AppIdentity.CODER: "agentic/coder",
                AppIdentity.REVIEWER: "agentic/reviewer",
            },
            client=fake_sm,
        )

        c1 = provider.get_credentials(AppIdentity.CODER)
        c2 = provider.get_credentials(AppIdentity.CODER)
        r1 = provider.get_credentials(AppIdentity.REVIEWER)
        assert c1 == c2
        assert c1.app_id == "111"
        assert r1.app_id == "222"
        assert fake_sm.calls == ["agentic/coder", "agentic/reviewer"]


class TestGitHubAppAuth:
    def test_mints_valid_jwt_and_caches_refreshes_repo_scoped_token(
        self,
        rsa_keys: dict[str, tuple[str, str]],
    ) -> None:
        coder_pem, coder_pub = rsa_keys["coder"]
        current_time = [datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC)]
        token_counter = [0]
        requests_seen: list[tuple[str, str, dict[str, Any] | None]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            auth_header = request.headers.get("Authorization", "")
            assert auth_header.startswith("Bearer ")
            decoded = jwt.decode(
                auth_header.removeprefix("Bearer "),
                coder_pub,
                algorithms=["RS256"],
                options={"verify_exp": False},
            )
            assert decoded["iss"] == "1001"
            body = json.loads(request.content) if request.content else None
            requests_seen.append((request.method, request.url.path, body))

            if request.url.path == "/repos/waustinlynn/demo/installation":
                return httpx.Response(200, json={"id": 9876})
            if request.url.path == "/app/installations/9876/access_tokens":
                token_counter[0] += 1
                expires = current_time[0] + timedelta(hours=1)
                return httpx.Response(
                    201,
                    json={
                        "token": f"ghs_token_{token_counter[0]}",
                        "expires_at": expires.isoformat().replace("+00:00", "Z"),
                    },
                )
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        provider = _StaticCredentialProvider(
            {
                AppIdentity.CODER: GitHubAppCredentials(
                    identity=AppIdentity.CODER,
                    app_id="1001",
                    private_key_pem=coder_pem,
                )
            }
        )
        auth = GitHubAppAuth(
            AppIdentity.CODER,
            provider,
            http_client=client,
            clock=lambda: current_time[0],
        )

        # First call resolves installation + mints token scoped to ["demo"].
        t1 = auth.get_installation_token("waustinlynn/demo")
        assert t1 == "ghs_token_1"
        assert requests_seen == [
            ("GET", "/repos/waustinlynn/demo/installation", None),
            ("POST", "/app/installations/9876/access_tokens", {"repositories": ["demo"]}),
        ]

        # Within cache window -> no additional HTTP requests.
        current_time[0] += timedelta(minutes=30)
        assert auth.get_installation_token("waustinlynn/demo") == "ghs_token_1"
        assert len(requests_seen) == 2

        # Inside 2-minute refresh margin -> refreshes token reusing cached installation_id.
        current_time[0] += timedelta(minutes=29)
        t2 = auth.get_installation_token("waustinlynn/demo")
        assert t2 == "ghs_token_2"
        assert len(requests_seen) == 3
        assert requests_seen[-1] == (
            "POST",
            "/app/installations/9876/access_tokens",
            {"repositories": ["demo"]},
        )


class TestGitHubClientOperations:
    def _build_pair(
        self,
        rsa_keys: dict[str, tuple[str, str]],
        handler: Callable[[httpx.Request], httpx.Response],
    ) -> tuple[GitHubClient, GitHubClient]:
        coder_pem, coder_pub = rsa_keys["coder"]
        reviewer_pem, reviewer_pub = rsa_keys["reviewer"]

        def wrapped_handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/installation"):
                token = request.headers["Authorization"].removeprefix("Bearer ")
                claims = jwt.decode(token, options={"verify_signature": False})
                if claims["iss"] == "1001":
                    jwt.decode(
                        token, coder_pub, algorithms=["RS256"], options={"verify_exp": False}
                    )
                    return httpx.Response(200, json={"id": 10})
                jwt.decode(token, reviewer_pub, algorithms=["RS256"], options={"verify_exp": False})
                return httpx.Response(200, json={"id": 20})
            if request.url.path == "/app/installations/10/access_tokens":
                return httpx.Response(
                    201, json={"token": "ghs_coder_token", "expires_at": "2099-01-01T00:00:00Z"}
                )
            if request.url.path == "/app/installations/20/access_tokens":
                return httpx.Response(
                    201, json={"token": "ghs_reviewer_token", "expires_at": "2099-01-01T00:00:00Z"}
                )
            return handler(request)

        http = httpx.Client(transport=httpx.MockTransport(wrapped_handler))
        provider = _StaticCredentialProvider(
            {
                AppIdentity.CODER: GitHubAppCredentials(AppIdentity.CODER, "1001", coder_pem),
                AppIdentity.REVIEWER: GitHubAppCredentials(
                    AppIdentity.REVIEWER, "2002", reviewer_pem
                ),
            }
        )
        pair = create_github_clients(provider, http_client=http)
        return pair.coder, pair.reviewer

    def test_swap_agent_labels_preserves_non_agent_and_paused_labels(
        self,
        rsa_keys: dict[str, tuple[str, str]],
    ) -> None:
        put_payloads: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["Authorization"] == "Bearer ghs_coder_token"
            if (
                request.method == "GET"
                and request.url.path == "/repos/waustinlynn/demo/issues/7/labels"
            ):
                return httpx.Response(
                    200,
                    json=[
                        {"name": "backend"},
                        {"name": "agent:ready"},
                        {"name": "agent:paused"},
                    ],
                )
            if (
                request.method == "PUT"
                and request.url.path == "/repos/waustinlynn/demo/issues/7/labels"
            ):
                body = json.loads(request.content)
                put_payloads.append(body)
                return httpx.Response(200, json=[{"name": n} for n in body["labels"]])
            return httpx.Response(404)

        coder, _ = self._build_pair(rsa_keys, handler)
        updated = coder.swap_agent_labels(
            "waustinlynn/demo",
            7,
            add=[AgentLabel.IN_PROGRESS],
            clear_other_agent_labels=True,
        )
        assert updated == ["backend", "agent:paused", "agent:in-progress"]
        assert put_payloads == [{"labels": ["backend", "agent:paused", "agent:in-progress"]}]

    def test_submit_pr_review_requires_reviewer_identity_and_formats_comments(
        self,
        rsa_keys: dict[str, tuple[str, str]],
    ) -> None:
        posted: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["Authorization"] == "Bearer ghs_reviewer_token"
            if (
                request.method == "POST"
                and request.url.path == "/repos/waustinlynn/demo/pulls/12/reviews"
            ):
                body = json.loads(request.content)
                posted.append(body)
                return httpx.Response(200, json={"id": 500, "state": body["event"]})
            return httpx.Response(404)

        coder, reviewer = self._build_pair(rsa_keys, handler)
        review = ReviewResult(
            verdict=ReviewVerdict.REQUEST_CHANGES,
            summary="Please tighten error handling.",
            comments=[
                ReviewComment(
                    path="src/app.py",
                    line=15,
                    severity=ReviewCommentSeverity.BLOCKER,
                    body="Handle HTTP 429 retries.",
                )
            ],
        )

        # Coder identity is blocked from submitting formal PR reviews by default.
        with pytest.raises(GitHubAPIError, match="agentic-reviewer"):
            coder.submit_pr_review("waustinlynn/demo", 12, review)

        resp = reviewer.submit_pr_review("waustinlynn/demo", 12, review, commit_id="abc123")
        assert resp["state"] == "REQUEST_CHANGES"
        assert posted == [
            {
                "event": "REQUEST_CHANGES",
                "body": "Please tighten error handling.",
                "commit_id": "abc123",
                "comments": [
                    {
                        "path": "src/app.py",
                        "line": 15,
                        "side": "RIGHT",
                        "body": "[blocker] Handle HTTP 429 retries.",
                    }
                ],
            }
        ]

    def test_enable_auto_merge_uses_graphql_mutation(
        self,
        rsa_keys: dict[str, tuple[str, str]],
    ) -> None:
        graphql_vars: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path == "/repos/waustinlynn/demo/pulls/9":
                return httpx.Response(200, json={"number": 9, "node_id": "PR_kwDO_123"})
            if request.method == "POST" and request.url.path == "/graphql":
                payload = json.loads(request.content)
                graphql_vars.append(payload["variables"])
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "enablePullRequestAutoMerge": {
                                "pullRequest": {
                                    "id": "PR_kwDO_123",
                                    "number": 9,
                                    "autoMergeRequest": {"mergeMethod": "SQUASH"},
                                }
                            }
                        }
                    },
                )
            return httpx.Response(404)

        coder, _ = self._build_pair(rsa_keys, handler)
        result = coder.enable_auto_merge("waustinlynn/demo", 9, merge_method=MergeMethod.SQUASH)
        assert result["number"] == 9
        assert graphql_vars == [{"pullRequestId": "PR_kwDO_123", "mergeMethod": "SQUASH"}]

    def test_ci_status_diff_and_comments(
        self,
        rsa_keys: dict[str, tuple[str, str]],
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/repos/waustinlynn/demo/pulls/4":
                if request.headers["Accept"] == "application/vnd.github.v3.diff":
                    return httpx.Response(200, text="diff --git a/a.py b/a.py\n+print('hi')\n")
                return httpx.Response(200, json={"number": 4, "head": {"sha": "deadbeef"}})
            if request.url.path == "/repos/waustinlynn/demo/commits/deadbeef/check-runs":
                return httpx.Response(
                    200,
                    json={
                        "check_runs": [
                            {"name": "ruff", "status": "completed", "conclusion": "success"},
                            {"name": "pytest", "status": "in_progress", "conclusion": None},
                        ]
                    },
                )
            if request.url.path == "/repos/waustinlynn/demo/commits/deadbeef/status":
                return httpx.Response(200, json={"statuses": []})
            if (
                request.method == "POST"
                and request.url.path == "/repos/waustinlynn/demo/issues/4/comments"
            ):
                return httpx.Response(
                    201, json={"id": 1, "body": json.loads(request.content)["body"]}
                )
            return httpx.Response(404)

        coder, _ = self._build_pair(rsa_keys, handler)
        assert "+print('hi')" in coder.get_pull_request_diff("waustinlynn/demo", 4)
        ci = coder.get_ci_status("waustinlynn/demo", "deadbeef")
        assert ci.state == "pending"
        assert ci.total_checks == 2
        assert ci.pending_checks == ["pytest"]
        assert ci.failed_checks == []
        comment = coder.post_comment("waustinlynn/demo", 4, "Working on it.")
        assert comment["body"] == "Working on it."

    def test_bootstrap_labels_is_idempotent(
        self,
        rsa_keys: dict[str, tuple[str, str]],
    ) -> None:
        ready_spec = next(s for s in AGENT_LABEL_SPECS if s.name is AgentLabel.READY)
        plan_spec = next(s for s in AGENT_LABEL_SPECS if s.name is AgentLabel.PLAN)
        created_names: list[str] = []
        patched_names: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path == "/repos/waustinlynn/demo/labels":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "name": ready_spec.name.value,
                            "color": ready_spec.color.lower(),
                            "description": ready_spec.description,
                        },
                        {
                            "name": plan_spec.name.value,
                            "color": "000000",  # drifted color
                            "description": plan_spec.description,
                        },
                    ],
                )
            if request.method == "POST" and request.url.path == "/repos/waustinlynn/demo/labels":
                created_names.append(json.loads(request.content)["name"])
                return httpx.Response(201, json={})
            if request.method == "PATCH" and request.url.path.startswith(
                "/repos/waustinlynn/demo/labels/"
            ):
                patched_names.append(json.loads(request.content)["new_name"])
                return httpx.Response(200, json={})
            return httpx.Response(404)

        coder, _ = self._build_pair(rsa_keys, handler)
        result = coder.bootstrap_labels("waustinlynn/demo")
        assert result.unchanged == [AgentLabel.READY.value]
        assert result.updated == [AgentLabel.PLAN.value]
        assert len(result.created) == len(AGENT_LABEL_SPECS) - 2
        assert patched_names == [AgentLabel.PLAN.value]
        assert set(created_names) == set(result.created)
