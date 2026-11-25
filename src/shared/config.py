"""Loader and validator for repository-level ``.agentic/config.yaml`` files."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from shared.models import RepoConfig

DEFAULT_CONFIG_RELPATH = Path(".agentic") / "config.yaml"


class ConfigError(ValueError):
    """Raised when ``.agentic/config.yaml`` cannot be read or fails schema validation."""


def _format_validation_error(err: ValidationError, source: str) -> str:
    lines = [f"Invalid repo config ({source}):"]
    for issue in err.errors():
        loc = ".".join(str(part) for part in issue["loc"]) or "<root>"
        lines.append(f"  - {loc}: {issue['msg']}")
    return "\n".join(lines)


def parse_repo_config(raw_yaml: str, *, source: str = ".agentic/config.yaml") -> RepoConfig:
    """Parse and validate a YAML string into a ``RepoConfig``."""
    try:
        parsed = yaml.safe_load(raw_yaml)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Malformed YAML in {source}: {exc}") from exc

    if parsed is None:
        return RepoConfig()

    if not isinstance(parsed, dict):
        raise ConfigError(
            f"Invalid repo config ({source}): top-level document must be a YAML mapping"
        )

    try:
        return RepoConfig.model_validate(parsed)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc, source)) from exc


def load_repo_config(config_path: Path | str) -> RepoConfig:
    """Read and validate a ``.agentic/config.yaml`` file from disk."""
    path = Path(config_path)
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")
    return parse_repo_config(path.read_text(encoding="utf-8"), source=str(path))


def load_repo_config_or_default(repo_root: Path | str) -> RepoConfig:
    """Load ``<repo_root>/.agentic/config.yaml``, or return defaults if the file is absent."""
    config_path = Path(repo_root) / DEFAULT_CONFIG_RELPATH
    if not config_path.exists():
        return RepoConfig()
    return load_repo_config(config_path)
