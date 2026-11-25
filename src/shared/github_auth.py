"""GitHub App authentication supporting dual identities (coder & reviewer).

Works identically when credentials come from local files/environment variables
or AWS Secrets Manager, and caches short-lived repo-scoped installation tokens.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Protocol

import boto3
import httpx
import jwt

GITHUB_API_BASE_URL = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"


class AppIdentity(StrEnum):
    """Distinct GitHub App identities used by the 2-app architecture."""

    CODER = "agentic-coder"
    REVIEWER = "agentic-reviewer"


class GitHubAuthError(RuntimeError):
    """Raised when GitHub App credentials or installation tokens cannot be obtained."""


@dataclass(frozen=True, slots=True)
class GitHubAppCredentials:
    """App ID and PEM-encoded RSA private key for a single GitHub App."""

    identity: AppIdentity
    app_id: str
    private_key_pem: str


class CredentialProvider(Protocol):
    """Loads GitHub App credentials for either ``CODER`` or ``REVIEWER``."""

    def get_credentials(self, identity: AppIdentity) -> GitHubAppCredentials: ...


class SecretsManagerClientProtocol(Protocol):
    """Minimal subset of the boto3 Secrets Manager client used by the provider."""

    def get_secret_value(self, *, SecretId: str) -> Mapping[str, Any]: ...


def _normalize_pem(raw_pem: str) -> str:
    cleaned = raw_pem.strip()
    if "\\n" in cleaned and "\n" not in cleaned:
        cleaned = cleaned.replace("\\n", "\n")
    if "BEGIN" not in cleaned or "PRIVATE KEY" not in cleaned:
        raise GitHubAuthError("Provided private key does not look like a PEM-encoded key")
    return cleaned


class EnvCredentialProvider:
    """Loads GitHub App credentials from environment variables and optional PEM files.

    Environment variables per identity:
    - ``CODER``:
      ``GITHUB_CODER_APP_ID`` plus ``GITHUB_CODER_PRIVATE_KEY`` or ``GITHUB_CODER_PRIVATE_KEY_PATH``
    - ``REVIEWER``:
      ``GITHUB_REVIEWER_APP_ID`` plus ``GITHUB_REVIEWER_PRIVATE_KEY`` or
      ``GITHUB_REVIEWER_PRIVATE_KEY_PATH``
    """

    _PREFIX_MAP: ClassVar[dict[AppIdentity, str]] = {
        AppIdentity.CODER: "GITHUB_CODER",
        AppIdentity.REVIEWER: "GITHUB_REVIEWER",
    }

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._env = os.environ if environ is None else environ

    def get_credentials(self, identity: AppIdentity) -> GitHubAppCredentials:
        prefix = self._PREFIX_MAP[identity]
        app_id = self._env.get(f"{prefix}_APP_ID", "").strip()
        if not app_id:
            raise GitHubAuthError(f"Missing required environment variable: {prefix}_APP_ID")

        inline_key = self._env.get(f"{prefix}_PRIVATE_KEY", "").strip()
        key_path = self._env.get(f"{prefix}_PRIVATE_KEY_PATH", "").strip()

        if inline_key:
            pem = _normalize_pem(inline_key)
        elif key_path:
            path = Path(key_path).expanduser()
            if not path.is_file():
                raise GitHubAuthError(f"Private key file not found for {identity.value}: {path}")
            pem = _normalize_pem(path.read_text(encoding="utf-8"))
        else:
            raise GitHubAuthError(
                f"Missing private key for {identity.value}: set either "
                f"{prefix}_PRIVATE_KEY or {prefix}_PRIVATE_KEY_PATH"
            )

        return GitHubAppCredentials(identity=identity, app_id=app_id, private_key_pem=pem)


class SecretsManagerCredentialProvider:
    """Loads GitHub App credentials from AWS Secrets Manager JSON secrets.

    Each secret must be a JSON object: ``{"app_id": "...", "private_key": "-----BEGIN ..."}``.
    """

    def __init__(
        self,
        secret_ids: Mapping[AppIdentity, str],
        *,
        client: SecretsManagerClientProtocol | None = None,
        region_name: str | None = None,
    ) -> None:
        self._secret_ids = dict(secret_ids)
        self._client = client
        self._region_name = region_name
        self._cache: dict[AppIdentity, GitHubAppCredentials] = {}

    def _get_client(self) -> SecretsManagerClientProtocol:
        if self._client is None:
            self._client = boto3.client("secretsmanager", region_name=self._region_name)
        return self._client

    def get_credentials(self, identity: AppIdentity) -> GitHubAppCredentials:
        if identity in self._cache:
            return self._cache[identity]

        secret_id = self._secret_ids.get(identity)
        if not secret_id:
            raise GitHubAuthError(f"No Secrets Manager secret configured for {identity.value}")

        response = self._get_client().get_secret_value(SecretId=secret_id)
        secret_string = response.get("SecretString")
        if not isinstance(secret_string, str) or not secret_string.strip():
            raise GitHubAuthError(f"Secret {secret_id!r} does not contain a non-empty SecretString")

        try:
            payload = json.loads(secret_string)
        except json.JSONDecodeError as exc:
            raise GitHubAuthError(f"Secret {secret_id!r} is not valid JSON") from exc

        if not isinstance(payload, dict):
            raise GitHubAuthError(f"Secret {secret_id!r} must be a JSON object")

        app_id = str(payload.get("app_id", "")).strip()
        raw_key = str(payload.get("private_key", "")).strip()
        if not app_id or not raw_key:
            raise GitHubAuthError(
                f"Secret {secret_id!r} must contain non-empty 'app_id' and 'private_key' fields"
            )

        creds = GitHubAppCredentials(
            identity=identity,
            app_id=app_id,
            private_key_pem=_normalize_pem(raw_key),
        )
        self._cache[identity] = creds
        return creds


@dataclass(frozen=True, slots=True)
class _CachedInstallationToken:
    token: str
    expires_at: datetime


class GitHubAppAuth:
    """Mints GitHub App JWTs and caches repo-scoped installation access tokens."""

    def __init__(
        self,
        identity: AppIdentity,
        credential_provider: CredentialProvider,
        *,
        http_client: httpx.Client | None = None,
        api_base_url: str = GITHUB_API_BASE_URL,
        clock: Callable[[], datetime] | None = None,
        refresh_margin_seconds: int = 120,
        jwt_ttl_seconds: int = 540,
    ) -> None:
        self.identity = identity
        self._credential_provider = credential_provider
        self._owns_http_client = http_client is None
        self._http = http_client or httpx.Client(timeout=30.0)
        self._api_base_url = api_base_url.rstrip("/")
        self._clock = clock or (lambda: datetime.now(UTC))
        self._refresh_margin = timedelta(seconds=refresh_margin_seconds)
        self._jwt_ttl_seconds = jwt_ttl_seconds
        self._installation_ids: dict[str, int] = {}
        self._tokens: dict[str, _CachedInstallationToken] = {}

    def close(self) -> None:
        if self._owns_http_client:
            self._http.close()

    def mint_app_jwt(self) -> str:
        """Mint a short-lived RS256 JWT identifying this GitHub App."""
        creds = self._credential_provider.get_credentials(self.identity)
        now = int(self._clock().timestamp())
        payload = {
            "iat": now - 60,
            "exp": now + self._jwt_ttl_seconds,
            "iss": creds.app_id,
        }
        return jwt.encode(payload, creds.private_key_pem, algorithm="RS256")

    def get_installation_token(self, repo_full_name: str, *, force_refresh: bool = False) -> str:
        """Return a cached or freshly minted repo-scoped installation token."""
        now = self._clock()
        cached = self._tokens.get(repo_full_name)
        if (
            not force_refresh
            and cached is not None
            and now + self._refresh_margin < cached.expires_at
        ):
            return cached.token

        owner, repo_name = self._split_repo(repo_full_name)
        app_jwt = self.mint_app_jwt()
        headers = {
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }

        installation_id = self._installation_ids.get(repo_full_name)
        if installation_id is None:
            inst_resp = self._http.get(
                f"{self._api_base_url}/repos/{owner}/{repo_name}/installation",
                headers=headers,
            )
            if inst_resp.status_code != 200:
                raise GitHubAuthError(
                    f"Failed to resolve installation for {repo_full_name} "
                    f"as {self.identity.value} (HTTP {inst_resp.status_code}): {inst_resp.text}"
                )
            installation_id = int(inst_resp.json()["id"])
            self._installation_ids[repo_full_name] = installation_id

        token_resp = self._http.post(
            f"{self._api_base_url}/app/installations/{installation_id}/access_tokens",
            headers=headers,
            json={"repositories": [repo_name]},
        )
        if token_resp.status_code != 201:
            raise GitHubAuthError(
                f"Failed to create installation token for {repo_full_name} "
                f"as {self.identity.value} (HTTP {token_resp.status_code}): {token_resp.text}"
            )

        body = token_resp.json()
        token = str(body["token"])
        expires_at = datetime.fromisoformat(str(body["expires_at"]).replace("Z", "+00:00"))
        self._tokens[repo_full_name] = _CachedInstallationToken(token=token, expires_at=expires_at)
        return token

    @staticmethod
    def _split_repo(repo_full_name: str) -> tuple[str, str]:
        parts = repo_full_name.split("/")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError(f"Expected 'owner/repo', got {repo_full_name!r}")
        return parts[0], parts[1]
