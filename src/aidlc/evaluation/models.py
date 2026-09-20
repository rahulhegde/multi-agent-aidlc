from statistics import fmean
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class QualityGate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    status: Literal["passed", "failed", "not_executed"]
    detail: str
    evidence_artifact_ids: list[str] = Field(default_factory=list)


class QualityGateReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile: Literal["python-stdlib-v1", "python-react-v1"] = "python-stdlib-v1"
    run_id: str
    repair_attempt: int = Field(default=0, ge=0)
    verdict: Literal["passed", "failed", "blocked"]
    gates: list[QualityGate]
    source_artifact_ids: list[str] = Field(default_factory=list)
    deferred_checks: list[str] = Field(
        default_factory=lambda: [
            "type checking",
            "integration and browser tests",
            "dependency and security scanning",
            "requirement-to-test coverage",
            "runsc/Kata comparison",
        ]
    )


class RubricScore(BaseModel):
    model_config = ConfigDict(extra="forbid")
    applicability: Literal["applicable", "not_applicable"] = "applicable"
    score: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    rationale: str = Field(min_length=3, max_length=2000)
    evidence_artifact_ids: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def validate_applicability(self):
        if self.applicability == "applicable" and self.score is None:
            raise ValueError("An applicable rubric requires a score")
        if self.applicability == "not_applicable" and self.score is not None:
            raise ValueError("A not-applicable rubric cannot publish a score")
        return self


class EvaluationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["evaluated", "not_evaluated"]
    requirement_coverage: RubricScore | None = None
    mvp_completeness: RubricScore | None = None
    usability: RubricScore | None = None
    architecture: RubricScore | None = None
    maintainability: RubricScore | None = None
    risk_acceptance: RubricScore | None = None
    summary: str = Field(min_length=3, max_length=4000)
    recommended_repairs: list[str] = Field(default_factory=list, max_length=20)
    # Attached by the trusted agent wrapper after model output validation so the
    # artifact presented for human decision contains the exact deterministic
    # evidence being accepted or returned for repair.
    quality_gates: QualityGateReport | None = None

    @model_validator(mode="after")
    def validate_scores(self):
        self.overall_score()
        return self

    def overall_score(self) -> float | None:
        values = [
            self.requirement_coverage,
            self.mvp_completeness,
            self.usability,
            self.architecture,
            self.maintainability,
            self.risk_acceptance,
        ]
        if self.status == "not_evaluated":
            if any(value is not None for value in values):
                raise ValueError("An unevaluated report cannot publish rubric scores")
            return None
        if any(value is None for value in values):
            raise ValueError("An evaluated report requires all six rubric judgments")
        applicable = [
            value.score
            for value in values
            if value is not None and value.applicability == "applicable"
        ]
        return fmean(score for score in applicable if score is not None) if applicable else None


def repair_decision(
    gates: QualityGateReport,
    evaluation: EvaluationReport,
    attempt: int,
    limit: int,
    threshold: float,
    human_decision: Literal["accept", "repair"] | None = None,
) -> Literal["passed", "deferred", "repair", "exhausted"]:
    score = evaluation.overall_score()
    # An explicit human verdict is authoritative. The evaluation artifact is
    # bound to the quality-gate snapshot, so acceptance covers both reports.
    if human_decision == "accept":
        return "passed"
    if human_decision == "repair":
        return "repair" if attempt < limit else "exhausted"
    # Missing evidence is not a defect an implementation agent can safely repair.
    if gates.verdict == "blocked":
        return "deferred"
    if gates.verdict == "failed":
        return "repair" if attempt < limit else "exhausted"
    if gates.verdict == "passed" and score is None:
        return "deferred"
    if gates.verdict == "passed" and score is not None and score >= threshold:
        return "passed"
    return "repair" if attempt < limit else "exhausted"
