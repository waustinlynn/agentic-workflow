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

    def test_retries_fresh_session_when_resume_conversation_not_found(
        self,
        tmp_path: Path,
    ) -> None:
        seen_cmds: list[list[str]] = []

        def fake_runner(
            cmd: list[str],
            _cwd: Path,
            _env: Mapping[str, str],
            _timeout: float,
        ) -> SubprocessOutcome:
            seen_cmds.append(list(cmd))
            if "--resume" in cmd:
                return SubprocessOutcome(
                    returncode=1,
                    stdout="",
                    stderr='conversation "conv-stale" not found (cannot resume)',
                )
            return SubprocessOutcome(
                returncode=0,
                stdout=json.dumps(
                    {
                        "event": "result",
                        "result": {
                            "conversation_id": "conv-fresh",
                            "status": "SUCCESS",
                            "response": '{"ok": true}',
                            "structured_output": {"ok": True},
                        },
                    }
                ),
                stderr="",
            )

        backend = AgyCliBackend(subprocess_runner=fake_runner)
        result = backend.run_turn(
            "Implement fix",
            tmp_path,
            schema={"type": "object"},
            conversation_id="conv-stale",
        )

        assert len(seen_cmds) == 2
        assert "--resume" in seen_cmds[0] and "conv-stale" in seen_cmds[0]
        assert "--resume" not in seen_cmds[1]
        assert result.conversation_id == "conv-fresh"
        assert result.structured_output == {"ok": True}


@dataclass
class ToolCall:
    name: str


class _FakeChatResponse:
    def __init__(
        self,
        *,
        text_val: str,
        structured: Any | None = None,
        chunks: list[Any] | None = None,
        delay: float = 0.0,
    ) -> None:
        self._text = text_val
        self._structured = structured
        self._chunks = chunks or []
        self._delay = delay
        self.cancelled = False

    async def resolve(self) -> list[Any]:
        if self._delay:
            await asyncio.sleep(self._delay)
        return list(self._chunks)

    async def text(self) -> str:
        return self._text

    async def structured_output(self) -> Any | None:
        return self._structured

    async def cancel(self) -> None:
        self.cancelled = True


class TestAntigravitySdkBackend:
    def test_runs_turn_in_headless_vertex_mode_and_parses_structured_output(
        self,
        tmp_path: Path,
    ) -> None:
        recorded: dict[str, Any] = {}

        class _FakeBuiltinTools:
            ASK_QUESTION = "ask_question"
            SCHEDULE = "schedule"
            GENERATE_IMAGE = "generate_image"
            CREATE_FILE = "create_file"
            EDIT_FILE = "edit_file"
            RUN_COMMAND = "run_command"

        class _FakeCapabilities:
            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = kwargs

        class _FakeLocalAgentConfig:
            def __init__(self, **kwargs: Any) -> None:
                recorded["config_kwargs"] = kwargs

        class _FakeAgent:
            def __init__(self, config: Any) -> None:
                self._config = config
                self.conversation_id = "sdk-conv-42"

            async def __aenter__(self) -> _FakeAgent:
                return self

            async def __aexit__(self, *_exc: Any) -> None:
                return None

            async def chat(self, prompt: str) -> _FakeChatResponse:
                recorded["prompt"] = prompt
                return _FakeChatResponse(
                    text_val='```json\n{"ok": true}\n```',
                    structured={"ok": True},
                    chunks=[ToolCall(name="list_directory"), ToolCall(name="view_file")],
                )

        fake_sdk = SimpleNamespace(
            Agent=_FakeAgent,
            LocalAgentConfig=_FakeLocalAgentConfig,
            CapabilitiesConfig=_FakeCapabilities,
            BuiltinTools=_FakeBuiltinTools,
        )
        backend = AntigravitySdkBackend(
            api_key="vertex-key",
            vertex=True,
            model="gemini-2.5-pro",
            app_data_dir=str(tmp_path / "app-data"),
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
        assert recorded["config_kwargs"]["workspaces"] == [str(tmp_path)]
        assert recorded["config_kwargs"]["conversation_id"] == "prev-1"
        assert recorded["config_kwargs"]["response_schema"] == {"type": "object"}
        assert "create_file" in recorded["config_kwargs"]["capabilities"].kwargs["disabled_tools"]
        assert "edit_file" in recorded["config_kwargs"]["capabilities"].kwargs["disabled_tools"]
        assert "run_command" in recorded["config_kwargs"]["capabilities"].kwargs["disabled_tools"]
        assert recorded["prompt"] == "Plan issue #1"

        assert result.conversation_id == "sdk-conv-42"
        assert result.structured_output == {"ok": True}
        assert result.tool_calls == ("list_directory", "view_file")

    def test_sdk_timeout_cancels_response_and_raises_agent_timeout_error(
        self,
        tmp_path: Path,
    ) -> None:
        slow_response = _FakeChatResponse(text_val="too late", delay=5.0)

        class _SlowAgent:
            conversation_id = None

            def __init__(self, _config: Any) -> None:
                pass

            async def __aenter__(self) -> _SlowAgent:
                return self

            async def __aexit__(self, *_exc: Any) -> None:
                return None

            async def chat(self, _prompt: str) -> _FakeChatResponse:
                return slow_response

        fake_sdk = SimpleNamespace(
            Agent=_SlowAgent,
            LocalAgentConfig=lambda **kw: kw,
        )
        backend = AntigravitySdkBackend(
            app_data_dir=str(tmp_path / "app-data"),
            sdk_module=fake_sdk,
        )
        with pytest.raises(AgentTimeoutError, match="timed out"):
            backend.run_turn("hello", tmp_path, timeout=0.05)
        assert slow_response.cancelled is True

    def test_retries_fresh_session_when_conversation_cannot_be_resumed(
        self,
        tmp_path: Path,
    ) -> None:
        configs_seen: list[dict[str, Any]] = []

        class _ResumableAgent:
            def __init__(self, config: dict[str, Any]) -> None:
                self._config = config
                self.conversation_id = "sdk-fresh-99"

            async def __aenter__(self) -> _ResumableAgent:
                if "conversation_id" in self._config:
                    cid = self._config["conversation_id"]
                    raise RuntimeError(
                        f"Failed to initialize conversation at ws://localhost:35155/. "
                        f'Stderr: Failed to create agent: conversation "{cid}" not found '
                        f'in "/tmp/antigravity_up7nsc1n" (cannot resume)'
                    )
                return self

            async def __aexit__(self, *_exc: Any) -> None:
                return None

            async def chat(self, _prompt: str) -> _FakeChatResponse:
                return _FakeChatResponse(
                    text_val='{"ok": true}',
                    structured={"ok": True},
                )

        def _make_config(**kwargs: Any) -> dict[str, Any]:
            configs_seen.append(dict(kwargs))
            return kwargs

        fake_sdk = SimpleNamespace(
            Agent=_ResumableAgent,
            LocalAgentConfig=_make_config,
        )
        backend = AntigravitySdkBackend(
            app_data_dir=str(tmp_path / "app-data"),
            sdk_module=fake_sdk,
        )

        result = backend.run_turn(
            "Implement issue #34",
            tmp_path,
            schema={"type": "object"},
            conversation_id="b4bce9a4c4df56783c7e57363aec7f40",
        )

        assert len(configs_seen) == 2
        assert configs_seen[0]["conversation_id"] == "b4bce9a4c4df56783c7e57363aec7f40"
        assert "conversation_id" not in configs_seen[1]
        assert result.conversation_id == "sdk-fresh-99"
        assert result.structured_output == {"ok": True}

    def test_does_not_retry_unrelated_session_init_errors(
        self,
        tmp_path: Path,
    ) -> None:
        attempts = 0

        class _BrokenAgent:
            def __init__(self, _config: Any) -> None:
                pass

            async def __aenter__(self) -> _BrokenAgent:
                nonlocal attempts
                attempts += 1
                raise RuntimeError("Authentication failed: invalid API key")

            async def __aexit__(self, *_exc: Any) -> None:
                return None

        fake_sdk = SimpleNamespace(
            Agent=_BrokenAgent,
            LocalAgentConfig=lambda **kw: kw,
        )
        backend = AntigravitySdkBackend(
            app_data_dir=str(tmp_path / "app-data"),
            sdk_module=fake_sdk,
        )

        with pytest.raises(RuntimeError, match="invalid API key"):
            backend.run_turn(
                "Implement issue #34",
                tmp_path,
                conversation_id="b4bce9a4c4df56783c7e57363aec7f40",
            )
        assert attempts == 1


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
