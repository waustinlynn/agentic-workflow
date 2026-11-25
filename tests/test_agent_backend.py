"""Unit tests for pluggable Antigravity backends (AgyCliBackend and AntigravitySdkBackend)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from worker.agent_backend import (
    AgentBackendError,
    AgentTimeoutError,
    AgyCliBackend,
    AntigravitySdkBackend,
    SubprocessOutcome,
    create_agent_backend,
)


class TestAgyCliBackend:
    def test_builds_flags_passes_api_key_and_parses_real_stream_json(self, tmp_path: Path) -> None:
        captured: dict[str, Any] = {}

        def fake_runner(
            cmd: list[str],
            cwd: Path,
            env: Mapping[str, str],
            timeout: float,
        ) -> SubprocessOutcome:
            captured["cmd"] = cmd
            captured["cwd"] = cwd
            captured["api_key"] = env.get("ANTIGRAVITY_API_KEY")
            captured["timeout"] = timeout
            ndjson = "\n".join(
                [
                    "warning: non-json stderr/stdout noise line",
                    json.dumps({"event": "init", "conversation_id": "conv-123"}),
                    json.dumps(
                        {
                            "event": "step_update",
                            "step_update": {
                                "conversation_id": "conv-123",
                                "step_type": "tool",
                                "state": "ACTIVE",
                                "tool_name": "read_file",
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "event": "step_update",
                            "step_update": {
                                "conversation_id": "conv-123",
                                "step_type": "tool",
                                "state": "DONE",
                                "tool_name": "read_file",
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "event": "step_update",
                            "step_update": {
                                "conversation_id": "conv-123",
                                "step_type": "tool",
                                "state": "DONE",
                                "tool_name": "run_command",
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "event": "result",
                            "result": {
                                "conversation_id": "conv-123",
                                "status": "SUCCESS",
                                "response": json.dumps(
                                    {"verdict": "APPROVE", "summary": "Clean diff"}
                                ),
                                "structured_output": {
                                    "verdict": "APPROVE",
                                    "summary": "Clean diff",
                                },
                            },
                        }
                    ),
                ]
            )
            return SubprocessOutcome(returncode=0, stdout=ndjson, stderr="")

        backend = AgyCliBackend(
            agy_bin="/usr/local/bin/agy",
            api_key="headless-key-xyz",
            sandbox=True,
            model="gemini-2.5-pro",
            subprocess_runner=fake_runner,
        )

        schema = {
            "type": "object",
            "properties": {"verdict": {"type": "string"}, "summary": {"type": "string"}},
            "required": ["verdict", "summary"],
        }
        result = backend.run_turn(
            "Review this diff",
            tmp_path,
            schema=schema,
            conversation_id="conv-prev",
            timeout=180.0,
            read_only=True,
        )

        cmd = captured["cmd"]
        assert cmd[0] == "/usr/local/bin/agy"
        assert "--output-format" in cmd and "stream-json" in cmd
        assert "--disable-slash-commands" in cmd
        assert "--sandbox" in cmd
        assert "--print-timeout" in cmd and "180s" in cmd
        assert "--mode" in cmd and "plan" in cmd
        assert "--model" in cmd and "gemini-2.5-pro" in cmd
        assert "--resume" in cmd and "conv-prev" in cmd
        assert "--json-schema" in cmd
        assert cmd[-2:] == ["--print", "Review this diff"]
        assert captured["cwd"] == tmp_path
        assert captured["api_key"] == "headless-key-xyz"

        assert result.conversation_id == "conv-123"
        assert result.tool_calls == ("read_file", "run_command")
        assert result.structured_output == {"verdict": "APPROVE", "summary": "Clean diff"}
        assert result.duration_seconds >= 0.0

    def test_raises_on_nonzero_exit_error_status_or_missing_schema_json(
        self,
        tmp_path: Path,
    ) -> None:
        fail_backend = AgyCliBackend(
            subprocess_runner=lambda *_args: SubprocessOutcome(
                returncode=1, stdout="", stderr="auth failed"
            )
        )
        with pytest.raises(AgentBackendError, match="exited with status 1"):
            fail_backend.run_turn("hello", tmp_path)

        timeout_status_backend = AgyCliBackend(
            subprocess_runner=lambda *_args: SubprocessOutcome(
                returncode=0,
                stdout=json.dumps(
                    {
                        "event": "result",
                        "result": {"status": "TIMEOUT", "error": "exceeded print-timeout"},
                    }
                ),
                stderr="",
            )
        )
        with pytest.raises(AgentTimeoutError, match="TIMEOUT"):
            timeout_status_backend.run_turn("hello", tmp_path)

        bad_json_backend = AgyCliBackend(
            subprocess_runner=lambda *_args: SubprocessOutcome(
                returncode=0,
                stdout=json.dumps(
                    {
                        "event": "result",
                        "result": {"status": "SUCCESS", "response": "not valid json"},
                    }
                ),
                stderr="",
            )
        )
        with pytest.raises(AgentBackendError, match="Expected structured JSON"):
            bad_json_backend.run_turn("hello", tmp_path, schema={"type": "object"})

    def test_propagates_timeout_error(self, tmp_path: Path) -> None:
        def timeout_runner(*_args: Any) -> SubprocessOutcome:
            raise AgentTimeoutError("timed out")

        backend = AgyCliBackend(subprocess_runner=timeout_runner)
        with pytest.raises(AgentTimeoutError, match="timed out"):
            backend.run_turn("hello", tmp_path, timeout=5.0)


@dataclass
class _FakeSdkResponse:
    text: str
    conversation_id: str = "sdk-conv-9"
    tool_calls: tuple[str, ...] = ("read_file",)
    structured_output: dict[str, Any] | None = None


class TestAntigravitySdkBackend:
    def test_runs_turn_in_headless_vertex_mode_and_parses_structured_output(
        self,
        tmp_path: Path,
    ) -> None:
        recorded: dict[str, Any] = {}

        class _FakeCapabilities:
            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = kwargs

        class _FakeLocalAgentConfig:
            def __init__(self, **kwargs: Any) -> None:
                recorded["config_kwargs"] = kwargs

        class _FakeAgent:
            def __init__(self, config: Any) -> None:
                self._config = config

            async def __aenter__(self) -> _FakeAgent:
                return self

            async def __aexit__(self, *_exc: Any) -> None:
                return None

            async def run(self, prompt: str, **kwargs: Any) -> _FakeSdkResponse:
                recorded["prompt"] = prompt
                recorded["run_kwargs"] = kwargs
                return _FakeSdkResponse(
                    text='```json\n{"ok": true}\n```',
                    conversation_id="sdk-conv-42",
                    tool_calls=("list_dir", "read_file"),
                )

        fake_sdk = SimpleNamespace(
            Agent=_FakeAgent,
            LocalAgentConfig=_FakeLocalAgentConfig,
            CapabilitiesConfig=_FakeCapabilities,
        )
        backend = AntigravitySdkBackend(
            api_key="vertex-key",
            vertex=True,
            model="gemini-2.5-pro",
            sdk_module=fake_sdk,
        )

        result = backend.run_turn(
            "Plan issue #1",
            tmp_path,
            schema={"type": "object"},
            conversation_id="prev-1",
            timeout=30.0,
            read_only=True,
        )

        assert recorded["config_kwargs"]["vertex"] is True
        assert recorded["config_kwargs"]["api_key"] == "vertex-key"
        assert recorded["config_kwargs"]["workspace"] == str(tmp_path)
        assert recorded["config_kwargs"]["capabilities"].kwargs == {"allow_edits": False}
        assert recorded["run_kwargs"]["conversation_id"] == "prev-1"
        assert recorded["run_kwargs"]["json_schema"] == {"type": "object"}

        assert result.conversation_id == "sdk-conv-42"
        assert result.structured_output == {"ok": True}
        assert result.tool_calls == ("list_dir", "read_file")

    def test_sdk_timeout_raises_agent_timeout_error(self, tmp_path: Path) -> None:
        class _SlowAgent:
            def __init__(self, _config: Any) -> None:
                pass

            async def __aenter__(self) -> _SlowAgent:
                return self

            async def __aexit__(self, *_exc: Any) -> None:
                return None

            async def run(self, _prompt: str, **_kwargs: Any) -> _FakeSdkResponse:
                await asyncio.sleep(5)
                return _FakeSdkResponse(text="too late")

        fake_sdk = SimpleNamespace(
            Agent=_SlowAgent,
            LocalAgentConfig=lambda **kw: kw,
        )
        backend = AntigravitySdkBackend(sdk_module=fake_sdk)
        with pytest.raises(AgentTimeoutError, match="timed out"):
            backend.run_turn("hello", tmp_path, timeout=0.05)


class TestBackendFactory:
    def test_selects_backend_from_environment(self) -> None:
        default_backend = create_agent_backend({})
        assert isinstance(default_backend, AgyCliBackend)

        sdk_backend = create_agent_backend(
            {"AGENT_BACKEND": "sdk", "ANTIGRAVITY_VERTEX": "true", "GEMINI_API_KEY": "k1"}
        )
        assert isinstance(sdk_backend, AntigravitySdkBackend)

        with pytest.raises(ValueError, match="Unsupported AGENT_BACKEND"):
            create_agent_backend({"AGENT_BACKEND": "unknown"})
