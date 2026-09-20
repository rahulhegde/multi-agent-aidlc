export type RunStatus =
  | "pending"
  | "running"
  | "input_required"
  | "completed"
  | "failed"
  | "canceled"
  | "blocked";

export interface Stage {
  name: string;
  status: RunStatus;
  error: string | null;
}

export interface WorkflowRun {
  project_id: string;
  run_id: string;
  idea: string;
  status: RunStatus;
  current_stage: string | null;
  created_at: string;
  updated_at: string;
  error: string | null;
  stages: Stage[];
}

export interface CapabilityCheck {
  name: string;
  status: "pass" | "warn" | "fail" | "unavailable";
  detail: string;
  required: boolean;
}

export interface PlatformReport {
  ready: boolean;
  degraded: boolean;
  os_id: string;
  os_version: string;
  architecture: string;
  kernel: string;
  checks: CapabilityCheck[];
}

export interface SandboxQualification {
  artifact_id: string;
  generated_at: string;
  baseline_qualified: boolean;
  microvm_qualified: boolean;
  runtimes: {
    runtime: string;
    status: "pass" | "fail" | "unavailable";
    checks: Record<string, boolean>;
    jobs: Record<string, {
      status: string;
      execution: { duration_ms: number; image: string | null; reason: string | null };
    }>;
    timings?: Record<string, { samples_ms: number[]; median_ms: number }> | null;
    relative_to_runc?: Record<string, number | null> | null;
  }[];
  comparison?: { repetitions: number; same_environment: boolean; timing_scope: string };
}

export interface ArtifactRecord {
  metadata: {
    artifact_id: string;
    kind: string;
    stage_id: string;
    producing_agent: string;
    created_at: string;
  };
  content_path: string;
}

export interface FinOpsReport {
  measurement_version: string;
  totals: {
    actual_input_tokens: number | null;
    actual_output_tokens: number | null;
    actual_total_tokens: number | null;
    actual_cost_usd: number | null;
  };
}

export interface WorkflowEvent {
  event_id: number;
  event_type: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface DelegatedTask {
  invocation_id: string;
  agent: string;
  stage: string;
  endpoint: string;
  context_id: string;
  task_id: string | null;
  state: string;
  attempts: number;
  repair_attempt: number;
}
export interface HumanInteraction {
  surface_id: string;
  run_id: string;
  stage: string;
  agent: string;
  task_id: string;
  context_id: string;
  prompt: { kind: "clarification" | "approval" | "evaluation_decision"; question: string };
  artifact_id: string;
  artifact_sha256: string;
  version: number;
  messages: unknown[];
  response: unknown | null;
}

export interface RubricScore {
  applicability: "applicable" | "not_applicable";
  score: number | null;
  rationale: string;
  evidence_artifact_ids: string[];
}

export interface EvaluationView {
  quality_gates: {
    artifact_id: string;
    profile: string;
    verdict: "passed" | "failed" | "blocked";
    repair_attempt: number;
    gates: { name: string; status: "passed" | "failed" | "not_executed";
      detail: string; evidence_artifact_ids: string[] }[];
    deferred_checks: string[];
  } | null;
  agent_evaluation: {
    artifact_id: string;
    status: "evaluated" | "not_evaluated";
    summary: string;
    requirement_coverage: RubricScore | null;
    mvp_completeness: RubricScore | null;
    usability: RubricScore | null;
    architecture: RubricScore | null;
    maintainability: RubricScore | null;
    risk_acceptance: RubricScore | null;
    recommended_repairs: string[];
    quality_gates: EvaluationView["quality_gates"];
  } | null;
  decision: { decision: string; overall_score: number | null } | null;
  repair_attempts: number;
}
