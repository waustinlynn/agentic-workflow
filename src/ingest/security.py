"""Public-repository webhook security checks (HMAC, allowlists, forks, kill switch)."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable, Mapping, Set
from typing import Any

from shared.labels import AgentLabel

SIGNATURE_PREFIX = "sha256="


def verify_github_signature(
    raw_body: bytes,
    signature_header: str | None,
    secret: str,
) -> bool:
    """Verify GitHub ``X-Hub-Signature-256`` header using constant-time HMAC-SHA256."""
    if not secret or not signature_header:
        return False
    if not signature_header.startswith(SIGNATURE_PREFIX):
        return False

    received_digest = signature_header.removeprefix(SIGNATURE_PREFIX).strip().lower()
    if not received_digest:
        return False

    expected_digest = hmac.new(
        secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected_digest, received_digest)


def is_actor_allowed(actor: str | None, allowed_actors: Set[str]) -> bool:
    """Return True if ``actor`` matches the configured allowlist (case-insensitive)."""
    if not actor:
        return False
    normalized_allowed = {entry.strip().lower() for entry in allowed_actors if entry.strip()}
    return actor.strip().lower() in normalized_allowed


def is_external_fork_pr(pr_payload: Mapping[str, Any], repo_full_name: str) -> bool:
    """Return True if a pull request originates from an external or deleted fork repository."""
    head = pr_payload.get("head")
    if not isinstance(head, Mapping):
        return True

    head_repo = head.get("repo")
    if not isinstance(head_repo, Mapping):
        # Deleted fork repositories arrive with head.repo == None.
        return True

    head_full_name = head_repo.get("full_name")
    if not isinstance(head_full_name, str) or not head_full_name.strip():
        return True

    return head_full_name.strip().lower() != repo_full_name.strip().lower()


def has_paused_label(labels: Iterable[Mapping[str, Any] | str]) -> bool:
    """Return True if the ``agent:paused`` kill-switch label is present."""
    paused = AgentLabel.PAUSED.value.lower()
    for label in labels:
        if isinstance(label, str):
            name = label
        elif isinstance(label, Mapping):
            raw_name = label.get("name")
            name = str(raw_name) if raw_name is not None else ""
        else:
            continue
        if name.strip().lower() == paused:
            return True
    return False
