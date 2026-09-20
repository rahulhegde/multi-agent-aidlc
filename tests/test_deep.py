"""A scripted model verifies the real graph without paid API calls."""

import asyncio
import json
from typing import Any

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from aidlc.agents.deep import migrate
from aidlc.config import Settings
from aidlc.domain.models import AgentContext, ArtifactKind, StageName
from tests.reference_agents import AGENTS_BY_STAGE


@pytest.mark.parametrize(
    "metadata, refusal, expected, calls",
    [
        (
            {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
            False,
            "increase AIDLC_MAX_IMPLEMENTATION_OUTPUT_TOKENS",
            1,
        ),
        ({"finish_reason": "length"}, False, "output token limit reached", 1),
        ({}, True, "model refused", 1),
        ({}, False, "structured-output tool result", 3),
    ],
)
def test_missing_source_output_has_actionable_bounded_failure(
    tmp_path,
    monkeypatch,
    metadata,
    refusal,
    expected,
    calls,
):
    from aidlc.agents import deep
    from aidlc.agents.catalog import AGENT_SPECS

    invocations = []
    message = AIMessage(
        content="",
        response_metadata=metadata,
        additional_kwargs={"refusal": "refused"} if refusal else {},
    )

    class Graph:
        async def ainvoke(self, state, **_kwargs):
            invocations.append(state)
            return {"messages": [message], "structured_response": None, "model_call_count": 1}

    monkeypatch.setattr(deep, "build_graph", lambda *_args, **_kwargs: Graph())
    spec = next(spec for spec in AGENT_SPECS if spec.name == "backend-agent")
    agent = migrate(spec, Settings(data_dir=tmp_path, model="test:scripted"))
    with pytest.raises(RuntimeError, match=expected):
        asyncio.run(
            agent.handler(
                AgentContext(
                    run_id="run_test",
                    idea="Build a task list",
                    stage=StageName.IMPLEMENTATION,
                )
            )
        )
    assert len(invocations) == calls
    if calls > 1:
        assert invocations[1]["model_call_count"] == 1


def test_missing_source_output_is_recovered(tmp_path, monkeypatch):
    from aidlc.agents import deep
    from aidlc.agents.catalog import AGENT_SPECS

    invocations = []

    class Graph:
        async def ainvoke(self, state, **_kwargs):
            invocations.append(state)
            if len(invocations) == 1:
                return {
                    "messages": [AIMessage(content="Here is the code.")],
                    "structured_response": None,
                }
            assert "SourceOutput" in state["messages"][-1].content
            return {
                "structured_response": {
                    "files": [{"path": "backend/api.py", "content": "answer = 42\n"}],
                }
            }

    monkeypatch.setattr(deep, "build_graph", lambda *_args, **_kwargs: Graph())
    spec = next(spec for spec in AGENT_SPECS if spec.name == "backend-agent")
    result = asyncio.run(
        migrate(spec, Settings(data_dir=tmp_path)).handler(
            AgentContext(
                run_id="run_test",
                idea="Build a task list",
                stage=StageName.IMPLEMENTATION,
            )
        )
    )
    assert len(invocations) == 2
    assert result.content["files"][0]["path"] == "backend/api.py"


@pytest.mark.parametrize("name, expected", [("intake-agent", 12345), ("backend-agent", 23456)])
def test_model_factory_uses_configured_output_limits(tmp_path, monkeypatch, name, expected):
    from aidlc.agents import deep
    from aidlc.agents.catalog import AGENT_SPECS

    options = {}

    def factory(_model, **kwargs):
        options.update(kwargs)
        return object()

    monkeypatch.setattr(deep, "init_chat_model", factory)
    monkeypatch.setattr(deep, "create_deep_agent", lambda **kwargs: kwargs["model"])
    deep.build_graph(
        Settings(
            data_dir=tmp_path,
            model="openai:gpt-5.6-luna",
            max_model_output_tokens=12345,
            max_implementation_output_tokens=23456,
        ),
        next(spec for spec in AGENT_SPECS if spec.name == name),
    )
    assert options["max_tokens"] == expected


def test_openai_luna_structured_output_uses_responses(tmp_path, monkeypatch):
    from aidlc.agents import deep

    original_factory = deep.init_chat_model
    requests = []
    brief = {
        "content": {
            "idea": "Build a task list",
            "target_user": "Students",
            "problem": "Track homework",
            "assumptions": [],
            "mvp_goal": "List homework",
        },
        "markdown": "# Student task list",
        "clarification": None,
    }

    def respond(request):
        payload = json.loads(request.content)
        requests.append(request)
        assert request.url.path == "/v1/responses"
        assert payload["model"] == "gpt-5.6-luna"
        assert any(tool.get("name") == "BriefOutput" for tool in payload["tools"])
        return httpx.Response(
            200,
            json={
                "id": "resp_test",
                "object": "response",
                "created_at": 0,
                "status": "completed",
                "model": "gpt-5.6-luna",
                "output": [
                    {
                        "type": "function_call",
                        "id": "fc_test",
                        "call_id": "call_test",
                        "name": "BriefOutput",
                        "arguments": json.dumps(brief),
                        "status": "completed",
                    }
                ],
                "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(
            deep,
            "init_chat_model",
            lambda model, **kwargs: original_factory(
                model,
                **kwargs,
                api_key="test-key",
                http_client=client,
            ),
        )
        monkeypatch.setattr(deep, "create_deep_agent", lambda **kwargs: kwargs["model"])
        selected: Any = deep.build_graph(
            Settings(data_dir=tmp_path, model="openai:gpt-5.6-luna"),
            AGENTS_BY_STAGE[StageName.INTAKE][0],
        )
        result = selected.bind_tools([deep.BriefOutput]).invoke("Build a task list")
    assert len(requests) == 1
    assert result.tool_calls[0]["args"]["content"]["target_user"] == "Students"
    assert result.usage_metadata["input_tokens"] == 100


def test_groq_factory_does_not_receive_openai_options(tmp_path, monkeypatch):
    from aidlc.agents import deep

    options = {}
    selected = object()

    def factory(model, **kwargs):
        assert model == "groq:openai/gpt-oss-120b"
        options.update(kwargs)
        return selected

    monkeypatch.setattr(deep, "init_chat_model", factory)
    monkeypatch.setattr(deep, "create_deep_agent", lambda **kwargs: kwargs["model"])
    graph = deep.build_graph(
        Settings(data_dir=tmp_path, model="groq:openai/gpt-oss-120b"),
        AGENTS_BY_STAGE[StageName.INTAKE][0],
    )
    assert graph is selected
    assert "use_responses_api" not in options


class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        native_tools = {
            "task",
            "ls",
            "read_file",
            "write_file",
            "edit_file",
            "glob",
            "grep",
            "execute",
            "write_todos",
        }
        assert not any(getattr(tool, "name", None) in native_tools for tool in tools)
        return self


def test_specialist_policy_preserves_configured_analysis_and_source_output(tmp_path):
    from langchain_core.messages import HumanMessage
    from langchain_core.tools import tool

    from aidlc.agents.catalog import AGENT_SPECS
    from aidlc.agents.deep import build_graph

    analyzed = []
    bindings = []

    @tool
    async def analyze_code(artifact_id: str) -> dict:
        """Analyze the supplied source artifact."""
        analyzed.append(artifact_id)
        return {"summary": "Reviewed input source"}

    class RecordingModel(ScriptedModel):
        def bind_tools(self, tools, **kwargs):
            bindings.append({getattr(tool, "name", None) for tool in tools})
            return super().bind_tools(tools, **kwargs)

    model = RecordingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "analyze_code",
                        "id": "analysis",
                        "type": "tool_call",
                        "args": {"artifact_id": "art_input"},
                    }
                ],
            ),
            AIMessage(
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
            ),
        ]
    )
    graph = build_graph(
        Settings(data_dir=tmp_path, model="test:scripted"),
        next(spec for spec in AGENT_SPECS if spec.name == "backend-agent"),
        model=model,
        tools=[analyze_code],
    )
    output = asyncio.run(
        graph.ainvoke({"messages": [HumanMessage(content="Implement the backend")]})
    )
    assert analyzed == ["art_input"]
    assert bindings == [{"analyze_code", "SourceOutput"}] * 2
    assert output["structured_response"].files[0].path == "backend/api.py"


def test_real_graph_recovers_missing_source_output(tmp_path):
    from aidlc.agents.catalog import AGENT_SPECS

    source = AIMessage(
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
    )
    spec = next(spec for spec in AGENT_SPECS if spec.name == "backend-agent")
    agent = migrate(
        spec,
        Settings(data_dir=tmp_path, model="test:scripted"),
        model=ScriptedModel(responses=[AIMessage(content="Here is the code."), source]),
    )
    result = asyncio.run(
        agent.handler(
            AgentContext(
                run_id="run_test",
                idea="Build a task list",
                stage=StageName.IMPLEMENTATION,
            )
        )
    )
    assert result.content["files"][0]["path"] == "backend/api.py"


def test_real_deep_graph_structured_output_and_usage(tmp_path):
    response = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "BriefOutput",
                "id": "brief",
                "type": "tool_call",
                "args": {
                    "content": {
                        "idea": "Build a task list",
                        "target_user": "Students",
                        "problem": "Track homework",
                        "assumptions": [],
                        "mvp_goal": "List homework",
                    },
                    "markdown": "# Student task list",
                    "clarification": "Which age group?",
                },
            }
        ],
        usage_metadata={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
        response_metadata={"model_name": "scripted-test"},
    )
    settings = Settings(
        data_dir=tmp_path,
        model="test:scripted",
        input_cost_per_million=1,
        output_cost_per_million=2,
    )
    agent = migrate(
        AGENTS_BY_STAGE[StageName.INTAKE][0], settings, model=ScriptedModel(responses=[response])
    )
    result = asyncio.run(
        agent.handler(
            AgentContext(run_id="run_test", idea="Build a task list", stage=StageName.INTAKE)
        )
    )
    assert result.artifact_kind == ArtifactKind.PROJECT_BRIEF
    assert result.content["target_user"] == "Students"
    assert result.human_prompt is not None and result.human_prompt.kind == "clarification"
    assert result.execution.input_tokens == 100
    assert result.execution.output_tokens == 50
    assert result.execution.cost_usd == pytest.approx(0.0002)
    assert result.execution.prompt_version == "aidlc-agent-goals-v4"


def test_native_delegation_is_rejected(tmp_path):
    from aidlc.agents.deep import build_graph

    with pytest.raises(ValueError, match="A2A"):
        build_graph(
            Settings(data_dir=tmp_path, general_purpose_subagent_enabled=True),
            AGENTS_BY_STAGE[StageName.INTAKE][0],
        )


def test_malformed_structured_output_has_bounded_repairs(tmp_path):
    invalid = AIMessage(
        content="",
        tool_calls=[
            {"name": "BriefOutput", "id": "bad", "type": "tool_call", "args": {"content": {}}}
        ],
    )
    agent = migrate(
        AGENTS_BY_STAGE[StageName.INTAKE][0],
        Settings(data_dir=tmp_path, model="test:scripted", max_repair_attempts=0),
        model=ScriptedModel(responses=[invalid]),
    )
    with pytest.raises(ValueError, match="repair limit"):
        asyncio.run(
            agent.handler(
                AgentContext(run_id="test", idea="Build a task list", stage=StageName.INTAKE)
            )
        )


@pytest.mark.parametrize("invented_reference", [False, True])
def test_real_evaluation_graph_uses_typed_rubric_and_known_evidence(tmp_path, invented_reference):
    from aidlc.evaluation.gates import evaluate_gates
    from tests.test_evaluation import Evidence, graded

    evidence = Evidence(tmp_path)
    gates = evaluate_gates(evidence.context())
    evidence.publish(
        ArtifactKind.QUALITY_GATE_REPORT,
        gates.model_dump(mode="json"),
        "deterministic-quality-gates",
    )
    content = graded(0.9).model_dump(mode="json")
    for key in (
        "requirement_coverage",
        "mvp_completeness",
        "usability",
        "architecture",
        "maintainability",
        "risk_acceptance",
    ):
        content[key]["evidence_artifact_ids"] = [
            "art_invented" if invented_reference else evidence.source["metadata"]["artifact_id"]
        ]
    response = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "EvaluationOutput",
                "id": "evaluation",
                "type": "tool_call",
                "args": {"content": content, "markdown": "# Evidence-based evaluation"},
            }
        ],
        usage_metadata={"input_tokens": 80, "output_tokens": 60, "total_tokens": 140},
        response_metadata={"model_name": "scripted-test"},
    )
    agent = migrate(
        AGENTS_BY_STAGE[StageName.EVALUATION][0],
        Settings(data_dir=tmp_path, model="test:scripted"),
        model=ScriptedModel(responses=[response]),
    )
    if invented_reference:
        with pytest.raises(ValueError, match="existing input artifact"):
            asyncio.run(agent.handler(evidence.context()))
    else:
        result = asyncio.run(agent.handler(evidence.context()))
        assert result.artifact_kind == ArtifactKind.EVALUATION_REPORT
        assert result.content["status"] == "evaluated"
        assert result.execution.input_tokens == 80
        assert result.execution.output_tokens == 60
        assert result.execution.prompt_version == "aidlc-agent-goals-v4"


def test_evaluation_repairs_tool_result_id_citation(tmp_path):
    from aidlc.evaluation.gates import evaluate_gates
    from tests.test_evaluation import Evidence, graded

    evidence = Evidence(tmp_path)
    gates = evaluate_gates(evidence.context())
    evidence.publish(
        ArtifactKind.QUALITY_GATE_REPORT,
        gates.model_dump(mode="json"),
        "deterministic-quality-gates",
    )
    corrected = graded(0.9).model_dump(mode="json")
    invalid = json.loads(json.dumps(corrected))
    for key in (
        "requirement_coverage",
        "mvp_completeness",
        "usability",
        "architecture",
        "maintainability",
        "risk_acceptance",
    ):
        invalid[key]["evidence_artifact_ids"] = ["art_from_analysis_id"]
        corrected[key]["evidence_artifact_ids"] = [evidence.source["metadata"]["artifact_id"]]

    def response(content, call_id):
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "EvaluationOutput",
                    "id": call_id,
                    "type": "tool_call",
                    "args": {"content": content, "markdown": "# Evaluation"},
                }
            ],
        )

    agent = migrate(
        AGENTS_BY_STAGE[StageName.EVALUATION][0],
        Settings(data_dir=tmp_path, model="test:scripted"),
        model=ScriptedModel(
            responses=[response(invalid, "invalid"), response(corrected, "corrected")]
        ),
    )

    result = asyncio.run(agent.handler(evidence.context()))

    assert result.content["risk_acceptance"]["evidence_artifact_ids"] == [
        evidence.source["metadata"]["artifact_id"]
    ]


def test_live_evaluation_calls_model_to_explain_missing_evidence(tmp_path):
    agent = migrate(
        AGENTS_BY_STAGE[StageName.EVALUATION][0],
        Settings(data_dir=tmp_path, model="test:scripted"),
        model=ScriptedModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "EvaluationOutput",
                            "id": "missing",
                            "type": "tool_call",
                            "args": {
                                "content": {
                                    "status": "not_evaluated",
                                    "summary": "No execution evidence",
                                },
                                "markdown": "# Missing execution evidence",
                            },
                        }
                    ],
                    usage_metadata={"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
                    response_metadata={"model_name": "scripted-test"},
                )
            ]
        ),
    )
    result = asyncio.run(
        agent.handler(
            AgentContext(
                run_id="run_missing",
                idea="Build a task list",
                stage=StageName.EVALUATION,
            )
        )
    )
    assert result.content["status"] == "not_evaluated"
    assert result.execution.input_tokens == 20


def test_all_catalog_roles_have_runtime_goal_contracts(monkeypatch, tmp_path):
    from aidlc.agents.catalog import AGENT_SPECS
    from aidlc.agents.deep import build_graph
    from aidlc.agents.profiles import GOAL_CONTRACTS, GOAL_VERSION, LEARNING_OUTPUT_POLICY

    assert set(GOAL_CONTRACTS) == {spec.name for spec in AGENT_SPECS}
    prompts = []
    monkeypatch.setattr(
        "aidlc.agents.deep.create_deep_agent",
        lambda **kwargs: prompts.append(kwargs["system_prompt"]),
    )
    for spec in AGENT_SPECS:
        build_graph(Settings(data_dir=tmp_path), spec, model=object())
        prompt = prompts[-1]
        assert spec.name in prompt
        assert GOAL_VERSION in prompt
        assert LEARNING_OUTPUT_POLICY in prompt
        assert GOAL_CONTRACTS[spec.name][0] in prompt
        assert "Produce a small, honest MVP specification" not in prompt
        assert "Do not claim code execution, testing, or deployment" not in prompt
