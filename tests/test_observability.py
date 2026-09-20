"""Verify diagnostics around real model conversion and graph failure paths without paid calls."""

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest
from langchain_core.messages import AIMessage

from aidlc.agents import deep
from aidlc.agents.catalog import AGENT_SPECS
from aidlc.config import Settings
from aidlc.domain.models import AgentContext, StageName
from tests.test_deep import ScriptedModel


def _spec(name="backend-agent"):
    return next(spec for spec in AGENT_SPECS if spec.name == name)


def _context():
    return AgentContext(
        run_id="run_diagnostics", idea="Private project prompt", stage=StageName.IMPLEMENTATION
    )


def _records(path: Path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def _source():
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "SourceOutput",
                "id": "source",
                "type": "tool_call",
                "args": {
                    "files": [{"path": "backend/api.py", "content": "answer = 42\n"}],
                },
            }
        ],
        usage_metadata={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
        response_metadata={"model_name": "scripted-test"},
    )


@pytest.mark.parametrize("reason", ["max_output_tokens", "max_messages", "content_filter", None])
def test_incomplete_openai_response_is_logged_before_missing_source_failure(
    tmp_path, monkeypatch, reason
):
    original_factory = deep.init_chat_model
    requests = []

    def respond(request):
        requests.append(request)
        assert request.url.path == "/v1/responses"
        payload = json.loads(request.content)
        assert {tool["name"] for tool in payload["tools"]} == {"SourceOutput"}
        assert payload["tool_choice"] == "required"
        assert "There is no workspace to inspect or edit" in json.dumps(payload["input"])
        return httpx.Response(
            200,
            json={
                "id": "resp_incomplete",
                "object": "response",
                "created_at": 0,
                "status": "incomplete",
                "model": "gpt-5.6-luna",
                "incomplete_details": {"reason": reason} if reason else None,
                "output": [
                    {
                        "type": "message",
                        "id": "msg_partial",
                        "role": "assistant",
                        "status": "incomplete",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Partial frontend code",
                                "annotations": [],
                            }
                        ],
                    }
                ],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 32768,
                    "total_tokens": 32868,
                    "output_tokens_details": {"reasoning_tokens": 32000},
                },
            },
        )

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            monkeypatch.setattr(
                deep,
                "init_chat_model",
                lambda model, **kwargs: original_factory(
                    model,
                    **kwargs,
                    api_key="test-key",
                    http_async_client=client,
                ),
            )
            agent = deep.migrate(
                _spec("frontend-agent"), Settings(data_dir=tmp_path, model="openai:gpt-5.6-luna")
            )
            with pytest.raises(RuntimeError) as failure:
                await agent.handler(_context())
            return str(failure.value)

    error = asyncio.run(exercise())
    assert len(requests) == 1
    assert "diagnostics=" in error and "response_id=resp_incomplete" in error
    assert (
        (reason or "reason not provided") in error
        if reason != "max_output_tokens"
        else "output token limit" in error
    )
    path = next((tmp_path / "logs" / _context().project_id).rglob("*.jsonl"))
    assert str(path) in error
    events = _records(path)
    completed = next(event for event in events if event["event"] == "llm.completed")
    message = completed["messages"][0]
    assert message["content"][0]["text"] == "Partial frontend code"
    assert message["response_metadata"]["status"] == "incomplete"
    assert message["usage_metadata"]["output_token_details"]["reasoning"] == 32000
    if reason:
        assert message["response_metadata"]["incomplete_details"]["reason"] == reason
    assert events[-1]["event"] == "invocation.failed"
    assert events[-1]["llm_calls"] == 1
    assert events[-1]["input_tokens"] == 100
    assert events[-1]["output_tokens"] == 32768
    assert "RuntimeError" in events[-1]["traceback"]
    assert _context().idea not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600


def test_malformed_tool_output_is_saved_even_when_graph_raises(tmp_path):
    invalid = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "SourceOutput",
                "id": "bad",
                "type": "tool_call",
                "args": {"files": []},
            }
        ],
        usage_metadata={"input_tokens": 5, "output_tokens": 6, "total_tokens": 11},
    )
    agent = deep.migrate(
        _spec(),
        Settings(data_dir=tmp_path, model="test:scripted", max_repair_attempts=0),
        model=ScriptedModel(responses=[invalid]),
    )
    with pytest.raises(ValueError, match="repair limit"):
        asyncio.run(agent.handler(_context()))
    events = _records(next((tmp_path / "logs").rglob("*.jsonl")))
    completed = next(event for event in events if event["event"] == "llm.completed")
    assert completed["messages"][0]["tool_calls"][0]["args"] == {"files": []}
    assert not any(event["event"] == "graph.completed" for event in events)
    assert events[-1]["event"] == "invocation.failed" and events[-1]["llm_calls"] == 1
    assert events[-1]["output_tokens"] == 6


def test_provider_failure_is_logged_without_retrying(tmp_path, monkeypatch):
    original_factory = deep.init_chat_model
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            500,
            json={
                "error": {"type": "server_error", "message": "Provider unavailable", "code": None},
            },
        )

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            monkeypatch.setattr(
                deep,
                "init_chat_model",
                lambda model, **kwargs: original_factory(
                    model,
                    **kwargs,
                    api_key="test-key",
                    http_async_client=client,
                ),
            )
            agent = deep.migrate(_spec(), Settings(data_dir=tmp_path, model="openai:gpt-5.6-luna"))
            with pytest.raises(Exception, match="Provider unavailable"):
                await agent.handler(_context())

    asyncio.run(exercise())
    assert len(requests) == 1
    events = _records(next((tmp_path / "logs").rglob("*.jsonl")))
    assert any(event["event"] == "llm.failed" for event in events)
    assert events[-1]["event"] == "invocation.failed" and events[-1]["llm_calls"] == 1
    assert "Provider unavailable" in events[-1]["traceback"]


def test_concurrent_invocations_have_distinct_logs_matching_execution_metadata(tmp_path):
    agent = deep.migrate(
        _spec(),
        Settings(data_dir=tmp_path, model="test:scripted"),
        model=ScriptedModel(responses=[_source()]),
    )

    async def exercise():
        return await asyncio.gather(agent.handler(_context()), agent.handler(_context()))

    results = asyncio.run(exercise())
    paths = list((tmp_path / "logs" / _context().project_id).rglob("*.jsonl"))
    assert len(paths) == 2
    assert {path.stem for path in paths} == {result.execution.execution_id for result in results}
    for path in paths:
        events = _records(path)
        assert all(event["execution_id"] == path.stem for event in events)
        assert events[-1]["event"] == "invocation.completed" and events[-1]["llm_calls"] == 1
        assert events[-1]["input_tokens"] == 10 and events[-1]["output_tokens"] == 20


def test_logs_redact_credentials_in_outputs_metadata_and_tracebacks(tmp_path, monkeypatch):
    secret = "diagnostic-secret-value"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    message = AIMessage(
        content=f"Returned code with {secret} and Bearer sensitive.jwt.token",
        response_metadata={"status": "incomplete", "headers": {"x-auth": "private"}},
        additional_kwargs={"api_key": "unknown-key", "encrypted_content": "hidden-reasoning"},
    )
    agent = deep.migrate(
        _spec(),
        Settings(data_dir=tmp_path, model="test:scripted"),
        model=ScriptedModel(responses=[message]),
    )
    with pytest.raises(RuntimeError):
        asyncio.run(agent.handler(_context()))
    text = next((tmp_path / "logs").rglob("*.jsonl")).read_text()
    for sensitive in [secret, "sensitive.jwt.token", "private", "unknown-key", "hidden-reasoning"]:
        assert sensitive not in text
    assert "Returned code" in text and "[redacted]" in text


@pytest.mark.parametrize("enabled", [False, True])
def test_disabled_or_unwritable_logs_do_not_change_success(tmp_path, monkeypatch, enabled):
    def denied(*args, **kwargs):
        raise PermissionError("Diagnostics directory unwritable")

    monkeypatch.setattr(os, "open", denied)
    agent = deep.migrate(
        _spec(),
        Settings(data_dir=tmp_path, model="test:scripted", llm_logging_enabled=enabled),
        model=ScriptedModel(responses=[_source()]),
    )
    result = asyncio.run(agent.handler(_context()))
    assert result.content["files"][0]["content"] == "answer = 42\n"
    assert not list((tmp_path / "logs").rglob("*.jsonl"))


def test_canceled_invocation_is_logged(tmp_path, monkeypatch):
    async def exercise():
        entered = asyncio.Event()

        class Graph:
            async def ainvoke(self, *args, **kwargs):
                entered.set()
                await asyncio.Event().wait()

        monkeypatch.setattr(deep, "build_graph", lambda *args, **kwargs: Graph())
        agent = deep.migrate(_spec(), Settings(data_dir=tmp_path))
        task = asyncio.ensure_future(agent.handler(_context()))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    events = _records(next((tmp_path / "logs").rglob("*.jsonl")))
    assert events[-1]["event"] == "invocation.canceled"
