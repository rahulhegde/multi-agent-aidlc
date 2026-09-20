import type { ArtifactRecord, DelegatedTask, EvaluationView, FinOpsReport, HumanInteraction, PlatformReport, SandboxQualification, WorkflowEvent, WorkflowRun } from "./types";

const API_BASE = import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8000";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    throw new Error(`${response.status}: ${await response.text()}`);
  }
  return response.json() as Promise<T>;
}

export const api = {
  config: () => request<{ execution_mode: string; model: string }>("/api/config"),
  interactions: (runId: string) => request<HumanInteraction[]>(`/api/runs/${runId}/interactions`),
  evaluation: (runId: string) => request<EvaluationView>(`/api/runs/${runId}/evaluation`),
  action: (interaction: HumanInteraction, decision: string, answer: string, actionId: string) =>
    request<HumanInteraction>(`/api/runs/${interaction.run_id}/actions`, {
      method: "POST",
      headers: { Authorization: `Bearer ${import.meta.env.VITE_HUMAN_ACTION_TOKEN ?? "local-learning-token"}` },
      body: JSON.stringify({ action_id: actionId, surface_id: interaction.surface_id,
        artifact_sha256: interaction.artifact_sha256, version: interaction.version, decision, answer }),
    }),
  platform: () => request<PlatformReport>("/api/platform"),
  sandboxes: () => request<SandboxQualification | null>("/api/platform/sandboxes"),
  runs: () => request<WorkflowRun[]>("/api/runs"),
  run: (runId: string) => request<WorkflowRun>(`/api/runs/${runId}`),
  createRun: (idea: string) =>
    request<WorkflowRun>("/api/runs", {
      method: "POST",
      body: JSON.stringify({ idea }),
    }),
  artifacts: (runId: string) =>
    request<ArtifactRecord[]>(`/api/runs/${runId}/artifacts`),
  finops: (runId: string) => request<FinOpsReport>(`/api/runs/${runId}/finops`),
  artifact: (artifactId: string) =>
    request<{ content: unknown }>(`/api/artifacts/${artifactId}`),
  history: (runId: string) => request<WorkflowEvent[]>(`/api/runs/${runId}/history`),
  tasks: (runId: string) => request<DelegatedTask[]>(`/api/runs/${runId}/tasks`),
  cancel: (runId: string) => request<{ accepted: boolean }>(
    `/api/runs/${runId}/cancel`, { method: "POST" }
  ),
  retry: (runId: string) => request<WorkflowRun>(
    `/api/runs/${runId}/retry`, { method: "POST" }
  ),
  eventsUrl: (runId: string) => `${API_BASE}/api/runs/${runId}/events`,
};
