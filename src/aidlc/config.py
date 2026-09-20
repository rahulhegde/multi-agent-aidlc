from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


def load_environment(path: Path | None = None) -> None:
    """All backend entry points share one file; exported environment takes precedence."""
    configured = path or Path(
        os.getenv("AIDLC_ENV_FILE", str(Path(__file__).resolve().parents[2] / ".env"))
    )
    load_dotenv(configured, override=False)


load_environment()


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    """Explicit application and harness defaults.

    Keeping defaults in code makes framework behavior visible and testable.
    Live profiles apply graph/model-call and repair limits; provider-specific
    reasoning and pre-call financial budget enforcement remain explicit gaps.
    """

    data_dir: Path = field(
        default_factory=lambda: Path(os.getenv("AIDLC_DATA_DIR", ".aidlc-data")).resolve()
    )
    required_host_os: str = "ubuntu"
    required_host_version: str = "26.04"
    enforce_host_preflight: bool = field(
        default_factory=lambda: _env_bool("AIDLC_ENFORCE_HOST_PREFLIGHT", True)
    )
    model: str = field(default_factory=lambda: os.getenv("AIDLC_MODEL", "provider:model"))
    agent_mode: str = field(default_factory=lambda: os.getenv("AIDLC_AGENT_MODE", "deep"))
    llm_logging_enabled: bool = field(
        default_factory=lambda: _env_bool("AIDLC_LLM_LOGGING_ENABLED", True)
    )
    human_gates_enabled: bool = field(default_factory=lambda: _env_bool("AIDLC_HUMAN_GATES", True))
    human_action_token: str = field(
        default_factory=lambda: os.getenv("AIDLC_HUMAN_ACTION_TOKEN", "local-learning-token")
    )
    input_cost_per_million: float | None = field(
        default_factory=lambda: (
            float(os.environ["AIDLC_INPUT_COST_PER_MILLION"])
            if "AIDLC_INPUT_COST_PER_MILLION" in os.environ
            else None
        )
    )
    output_cost_per_million: float | None = field(
        default_factory=lambda: (
            float(os.environ["AIDLC_OUTPUT_COST_PER_MILLION"])
            if "AIDLC_OUTPUT_COST_PER_MILLION" in os.environ
            else None
        )
    )
    reasoning_effort: str = "medium"
    max_model_output_tokens: int = field(
        default_factory=lambda: int(os.getenv("AIDLC_MAX_MODEL_OUTPUT_TOKENS", "16384"))
    )
    max_implementation_output_tokens: int = field(
        default_factory=lambda: int(os.getenv("AIDLC_MAX_IMPLEMENTATION_OUTPUT_TOKENS", "32768"))
    )
    max_agent_steps: int = 20
    max_parallel_agents: int = 3
    task_timeout_seconds: int = 600
    a2a_base_url: str = field(
        default_factory=lambda: os.getenv("AIDLC_A2A_BASE_URL", "http://127.0.0.1:8001")
    )
    a2a_max_retries: int = 2
    a2a_retry_delay_seconds: float = 0.1
    a2a_poll_interval_seconds: float = 0.1
    a2a_cancel_timeout_seconds: float = 5.0
    mcp_enabled: bool = field(default_factory=lambda: _env_bool("AIDLC_MCP_ENABLED", True))
    mcp_url: str = field(
        default_factory=lambda: os.getenv("AIDLC_MCP_URL", "http://127.0.0.1:8002/mcp")
    )
    oidc_issuer: str = field(
        default_factory=lambda: os.getenv("AIDLC_OIDC_ISSUER", "http://127.0.0.1:8080/realms/aidlc")
    )
    mcp_client_id: str = field(
        default_factory=lambda: os.getenv("AIDLC_MCP_CLIENT_ID", "static-analysis-agent")
    )
    mcp_client_secret: str = field(default_factory=lambda: os.getenv("AIDLC_MCP_CLIENT_SECRET", ""))
    analysis_timeout_seconds: int = 60
    analysis_max_parallel_jobs: int = 2
    analysis_max_output_bytes: int = 262_144
    analysis_backend: str = field(
        default_factory=lambda: os.getenv("AIDLC_ANALYSIS_BACKEND", "sandbox")
    )
    sandbox_image: str = field(default_factory=lambda: os.getenv("AIDLC_SANDBOX_IMAGE", ""))
    sandbox_execution_enabled: bool = field(
        default_factory=lambda: _env_bool("AIDLC_SANDBOX_EXECUTION_ENABLED", True)
    )
    max_repair_attempts: int = 2
    max_evaluation_repair_attempts: int = field(
        default_factory=lambda: int(os.getenv("AIDLC_MAX_EVALUATION_REPAIR_ATTEMPTS", "2"))
    )
    evaluation_score_threshold: float = field(
        default_factory=lambda: float(os.getenv("AIDLC_EVALUATION_SCORE_THRESHOLD", "0.8"))
    )
    max_clarification_rounds: int = 2
    token_budget_per_phase_per_run: int = field(
        default_factory=lambda: int(os.getenv("AIDLC_TOKEN_BUDGET_PER_PHASE_PER_RUN", "100000"))
    )
    sandbox_backend: str = field(default_factory=lambda: os.getenv("AIDLC_SANDBOX_BACKEND", "runc"))
    sandbox_network_enabled: bool = False
    require_kvm_for_microvm: bool = True
    a2ui_protocol_version: str = "0.9.1"
    general_purpose_subagent_enabled: bool = False
    require_requirements_approval: bool = True
    require_plan_approval: bool = True
    require_release_approval: bool = True
    cors_origins: tuple[str, ...] = ("http://127.0.0.1:5173", "http://localhost:5173")

    def __post_init__(self) -> None:
        if self.token_budget_per_phase_per_run <= 0:
            raise ValueError("Phase token budget per run must be greater than zero")
        if self.max_model_output_tokens <= 0 or self.max_implementation_output_tokens <= 0:
            raise ValueError("Model output token limits must be greater than zero")
        if self.max_repair_attempts < 0:
            raise ValueError("Structured-output repair attempts must not be negative")
        if not 0 <= self.max_evaluation_repair_attempts <= 10:
            raise ValueError("Evaluation repair attempts must be between 0 and 10")
        if not 0 <= self.evaluation_score_threshold <= 1:
            raise ValueError("Evaluation score threshold must be between 0 and 1")

    @property
    def database_path(self) -> Path:
        return self.data_dir / "aidlc.sqlite3"

    @property
    def artifact_dir(self) -> Path:
        return self.data_dir / "artifacts"
