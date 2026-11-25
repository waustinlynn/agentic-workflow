"""Pluggable Google Antigravity execution driver (``agy`` CLI and Python SDK)."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import subprocess
import tempfile
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


class AgentBackendError(RuntimeError):
    """Raised when an Antigravity turn fails or produces invalid structured output."""


class AgentTimeoutError(AgentBackendError):
    """Raised when an Antigravity turn exceeds its configured timeout."""


@dataclass(frozen=True, slots=True)
class AgentTurnResult:
    """Normalized result returned by any ``AgentBackend`` implementation."""

    text: str
    structured_output: dict[str, Any] | None = None
    conversation_id: str | None = None
    tool_calls: tuple[str, ...] = ()
    duration_seconds: float = 0.0
    raw_events: tuple[dict[str, Any], ...] = field(default=(), repr=False)


class AgentBackend(ABC):
    """Abstract interface decoupling worker orchestration from Antigravity transport/auth."""

    @abstractmethod
    def run_turn(
        self,
        prompt: str,
        workspace_dir: Path,
        *,
        schema: dict[str, Any] | Path | None = None,
        conversation_id: str | None = None,
        timeout: float = 600.0,
        read_only: bool = False,
    ) -> AgentTurnResult:
        """Execute a single autonomous agent turn inside ``workspace_dir``."""


@dataclass(frozen=True, slots=True)
class SubprocessOutcome:
    """Minimal subprocess return value used as an injectable test seam."""

    returncode: int
    stdout: str
    stderr: str


SubprocessRunner = Callable[
    [list[str], Path, Mapping[str, str], float],
    SubprocessOutcome,
]


def _default_subprocess_runner(
    cmd: list[str],
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
) -> SubprocessOutcome:
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=dict(env),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AgentTimeoutError(
            f"Antigravity CLI timed out after {timeout:.1f}s (cmd={cmd[0]!r})"
        ) from exc
    return SubprocessOutcome(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _extract_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped:
        return None
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            stripped = "\n".join(lines[1:-1]).strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


@dataclass(slots=True)
class _ParsedStream:
    text: str
    structured_output: dict[str, Any] | None
    conversation_id: str | None
    tool_calls: list[str]
    raw_events: list[dict[str, Any]]
    status: str | None
    error_message: str | None


def _parse_stream_json(stdout: str) -> _ParsedStream:
    """Parse NDJSON events emitted by ``agy --output-format stream-json --print ...``."""
    raw_events: list[dict[str, Any]] = []
    tool_calls: list[str] = []
    text_chunks: list[str] = []
    conversation_id: str | None = None
    final_result_text: str | None = None
    structured_output: dict[str, Any] | None = None
    result_status: str | None = None
    error_message: str | None = None

    for line in stdout.splitlines():
        cleaned = line.strip()
        if not cleaned or not cleaned.startswith("{"):
            continue
        try:
            event = json.loads(cleaned)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        raw_events.append(event)

        top_cid = event.get("conversation_id")
        if isinstance(top_cid, str) and top_cid.strip():
            conversation_id = top_cid.strip()

        event_kind = str(event.get("event") or event.get("type") or "").lower()

        if event_kind == "step_update":
            step = event.get("step_update")
            if isinstance(step, dict):
                step_cid = step.get("conversation_id")
                if isinstance(step_cid, str) and step_cid.strip():
                    conversation_id = step_cid.strip()
                step_type = str(step.get("step_type") or "").lower()
                state = str(step.get("state") or "").upper()
                if step_type == "tool" and state == "DONE":
                    tool_name = step.get("tool_name")
                    if isinstance(tool_name, str) and tool_name:
                        tool_calls.append(tool_name)
                elif step_type == "agent_response":
                    delta = step.get("text_delta")
                    if isinstance(delta, str) and delta:
                        text_chunks.append(delta)

        elif event_kind == "result":
            res_obj = event.get("result")
            if isinstance(res_obj, dict):
                res_cid = res_obj.get("conversation_id")
                if isinstance(res_cid, str) and res_cid.strip():
                    conversation_id = res_cid.strip()
                status_val = res_obj.get("status")
                if isinstance(status_val, str):
                    result_status = status_val.upper()
                err_val = res_obj.get("error") or res_obj.get("error_message")
                if isinstance(err_val, str) and err_val.strip():
                    error_message = err_val.strip()
                so = res_obj.get("structured_output")
                if isinstance(so, dict):
                    structured_output = so
                response_text = res_obj.get("response")
                if isinstance(response_text, str):
                    final_result_text = response_text
            elif isinstance(res_obj, str):
                final_result_text = res_obj

    final_text = (
        final_result_text if final_result_text is not None else "".join(text_chunks)
    ).strip()
    if structured_output is None:
        structured_output = _extract_json_object(final_text)

    return _ParsedStream(
        text=final_text,
        structured_output=structured_output,
        conversation_id=conversation_id,
        tool_calls=tool_calls,
        raw_events=raw_events,
        status=result_status,
        error_message=error_message,
    )


class AgyCliBackend(AgentBackend):
    """Runs Google Antigravity non-interactively via the ``agy`` CLI."""

    def __init__(
        self,
        *,
        agy_bin: str = "agy",
        api_key: str | None = None,
        sandbox: bool = True,
        auto_approve_permissions: bool = False,
        model: str | None = None,
        extra_env: Mapping[str, str] | None = None,
        subprocess_runner: SubprocessRunner | None = None,
    ) -> None:
        self._agy_bin = agy_bin
        self._api_key = api_key
        self._sandbox = sandbox
        self._auto_approve_permissions = auto_approve_permissions
        self._model = model
        self._extra_env = dict(extra_env or {})
        self._runner = subprocess_runner or _default_subprocess_runner

    def run_turn(
        self,
        prompt: str,
        workspace_dir: Path,
        *,
        schema: dict[str, Any] | Path | None = None,
        conversation_id: str | None = None,
        timeout: float = 600.0,
        read_only: bool = False,
    ) -> AgentTurnResult:
        if not workspace_dir.is_dir():
            raise AgentBackendError(f"Workspace directory does not exist: {workspace_dir}")

        temp_schema_file: Path | None = None
        schema_arg: str | None = None
        if isinstance(schema, Path):
            schema_arg = str(schema)
        elif isinstance(schema, dict):
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".schema.json",
                encoding="utf-8",
                delete=False,
            ) as tmp:
                json.dump(schema, tmp)
                temp_schema_file = Path(tmp.name)
                schema_arg = str(temp_schema_file)

        try:
            cmd = self._build_command(
                prompt=prompt,
                schema_arg=schema_arg,
                conversation_id=conversation_id,
                timeout=timeout,
                read_only=read_only,
            )
            env = dict(os.environ)
            env.update(self._extra_env)
            if self._api_key:
                env["ANTIGRAVITY_API_KEY"] = self._api_key

            started = time.monotonic()
            # Give outer subprocess extra headroom over --print-timeout so agy exits cleanly.
            outcome = self._runner(cmd, workspace_dir, env, timeout + 15.0)
            elapsed = time.monotonic() - started
        finally:
            if temp_schema_file is not None and temp_schema_file.exists():
                temp_schema_file.unlink()

        if outcome.returncode != 0:
            stderr_preview = outcome.stderr.strip()[:500]
            raise AgentBackendError(
                f"agy exited with status {outcome.returncode}: {stderr_preview}"
            )

        parsed = _parse_stream_json(outcome.stdout)
        if parsed.status is not None and parsed.status != "SUCCESS":
            detail = parsed.error_message or parsed.text[:300]
            if parsed.status == "TIMEOUT":
                raise AgentTimeoutError(f"agy reported TIMEOUT: {detail}")
            raise AgentBackendError(f"agy reported status={parsed.status}: {detail}")

        if schema is not None and parsed.structured_output is None:
            raise AgentBackendError(
                "Expected structured JSON output matching schema, but could not parse JSON "
                f"from agy output: {parsed.text[:300]!r}"
            )

        return AgentTurnResult(
            text=parsed.text,
            structured_output=parsed.structured_output,
            conversation_id=parsed.conversation_id or conversation_id,
            tool_calls=tuple(parsed.tool_calls),
            duration_seconds=elapsed,
            raw_events=tuple(parsed.raw_events),
        )

    def _build_command(
        self,
        *,
        prompt: str,
        schema_arg: str | None,
        conversation_id: str | None,
        timeout: float,
        read_only: bool,
    ) -> list[str]:
        cmd = [
            self._agy_bin,
            "--output-format",
            "stream-json",
            "--disable-slash-commands",
            "--print-timeout",
            f"{max(1, int(timeout))}s",
        ]
        if self._sandbox:
            cmd.append("--sandbox")
        if read_only:
            cmd.extend(["--mode", "plan"])
        else:
            cmd.extend(["--mode", "accept-edits"])
            if self._auto_approve_permissions:
                cmd.append("--dangerously-skip-permissions")
        if self._model:
            cmd.extend(["--model", self._model])
        if schema_arg:
            cmd.extend(["--json-schema", schema_arg])
        if conversation_id:
            cmd.extend(["--resume", conversation_id])
        # Always pass --print last with its attached prompt value so agy never misparses flags.
        cmd.extend(["--print", prompt])
        return cmd


class AntigravitySdkBackend(AgentBackend):
    """Wraps the ``google-antigravity`` Python SDK (local OAuth or headless Vertex/API key)."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        vertex: bool = False,
        model: str | None = None,
        sdk_module: Any | None = None,
    ) -> None:
        self._api_key = api_key
        self._vertex = vertex
        self._model = model
        self._sdk_module = sdk_module

    def _load_sdk(self) -> Any:
        if self._sdk_module is not None:
            return self._sdk_module
        try:
            return importlib.import_module("antigravity")
        except ImportError as exc:
            raise AgentBackendError(
                "google-antigravity Python SDK is not installed; install it or set "
                "AGENT_BACKEND=agy_cli"
            ) from exc

    def run_turn(
        self,
        prompt: str,
        workspace_dir: Path,
        *,
        schema: dict[str, Any] | Path | None = None,
        conversation_id: str | None = None,
        timeout: float = 600.0,
        read_only: bool = False,
    ) -> AgentTurnResult:
        if not workspace_dir.is_dir():
            raise AgentBackendError(f"Workspace directory does not exist: {workspace_dir}")

        schema_dict: dict[str, Any] | None = None
        if isinstance(schema, Path):
            schema_dict = json.loads(schema.read_text(encoding="utf-8"))
        elif isinstance(schema, dict):
            schema_dict = schema

        sdk = self._load_sdk()
        started = time.monotonic()

        async def _execute() -> AgentTurnResult:
            local_cfg_kwargs: dict[str, Any] = {
                "workspace": str(workspace_dir),
            }
            if self._vertex:
                local_cfg_kwargs["vertex"] = True
            if self._api_key:
                local_cfg_kwargs["api_key"] = self._api_key
            if self._model:
                local_cfg_kwargs["model"] = self._model

            capabilities_cls = getattr(sdk, "CapabilitiesConfig", None)
            if capabilities_cls is not None:
                local_cfg_kwargs["capabilities"] = capabilities_cls(
                    allow_edits=not read_only,
                )

            config_cls = sdk.LocalAgentConfig
            agent_cls = sdk.Agent
            config = config_cls(**local_cfg_kwargs)

            async with agent_cls(config) as agent:
                run_kwargs: dict[str, Any] = {}
                if conversation_id:
                    run_kwargs["conversation_id"] = conversation_id
                if schema_dict is not None:
                    run_kwargs["json_schema"] = schema_dict

                raw_response = await asyncio.wait_for(
                    agent.run(prompt, **run_kwargs),
                    timeout=timeout,
                )

            text = str(getattr(raw_response, "text", raw_response))
            structured = getattr(raw_response, "structured_output", None)
            if not isinstance(structured, dict):
                structured = _extract_json_object(text)
            if schema_dict is not None and structured is None:
                raise AgentBackendError(
                    "Expected structured JSON output from Antigravity SDK, "
                    f"got unparseable text: {text[:300]!r}"
                )

            resp_cid = getattr(raw_response, "conversation_id", None) or conversation_id
            resp_tools = tuple(str(t) for t in getattr(raw_response, "tool_calls", ()))
            return AgentTurnResult(
                text=text,
                structured_output=structured,
                conversation_id=str(resp_cid) if resp_cid else None,
                tool_calls=resp_tools,
                duration_seconds=time.monotonic() - started,
            )

        try:
            return asyncio.run(_execute())
        except TimeoutError as exc:
            raise AgentTimeoutError(f"Antigravity SDK timed out after {timeout:.1f}s") from exc


BackendKind = Literal["agy_cli", "sdk"]


def create_agent_backend(environ: Mapping[str, str] | None = None) -> AgentBackend:
    """Instantiate the configured ``AgentBackend`` from environment variables.

    - ``AGENT_BACKEND``: ``agy_cli`` (default) or ``sdk``
    - ``ANTIGRAVITY_API_KEY`` / ``GEMINI_API_KEY``: optional headless API key
    - ``ANTIGRAVITY_VERTEX``: ``true``/``1`` to enable Vertex mode on SDK backend
    - ``ANTIGRAVITY_MODEL``: optional model override
    - ``AGY_BIN``: path/name of ``agy`` binary (default ``agy``)
    - ``AGY_SANDBOX``: ``true`` (default) or ``false``
    - ``AGY_AUTO_APPROVE``: ``true``/``1`` to pass ``--dangerously-skip-permissions``
    """
    env = os.environ if environ is None else environ
    kind = env.get("AGENT_BACKEND", "agy_cli").strip().lower()
    api_key = (
        env.get("ANTIGRAVITY_API_KEY", "").strip() or env.get("GEMINI_API_KEY", "").strip() or None
    )
    model = env.get("ANTIGRAVITY_MODEL", "").strip() or None

    if kind == "agy_cli":
        sandbox = env.get("AGY_SANDBOX", "true").strip().lower() not in {"0", "false", "no"}
        auto_approve = env.get("AGY_AUTO_APPROVE", "false").strip().lower() in {"1", "true", "yes"}
        agy_bin = env.get("AGY_BIN", "agy").strip() or "agy"
        return AgyCliBackend(
            agy_bin=agy_bin,
            api_key=api_key,
            sandbox=sandbox,
            auto_approve_permissions=auto_approve,
            model=model,
        )

    if kind == "sdk":
        vertex = env.get("ANTIGRAVITY_VERTEX", "false").strip().lower() in {"1", "true", "yes"}
        return AntigravitySdkBackend(
            api_key=api_key,
            vertex=vertex,
            model=model,
        )

    raise ValueError(f"Unsupported AGENT_BACKEND={kind!r}; expected 'agy_cli' or 'sdk'")
