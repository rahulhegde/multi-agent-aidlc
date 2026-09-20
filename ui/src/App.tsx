import { FormEvent, useEffect, useState } from "react";
import { api } from "./api";
import { HumanSurface } from "./HumanSurface";
import { EvaluationPanel } from "./EvaluationPanel";
import type { EvaluationView, HumanInteraction } from "./types";
import type { ArtifactRecord, DelegatedTask, FinOpsReport, PlatformReport, SandboxQualification, WorkflowEvent, WorkflowRun } from "./types";

const TERMINAL = new Set(["completed", "failed", "canceled", "blocked"]);

export function App() {
  const [idea, setIdea] = useState("Build a tiny task list for a study group");
  const [platform, setPlatform] = useState<PlatformReport | null>(null);
  const [sandboxes, setSandboxes] = useState<SandboxQualification | null>(null);
  const [config, setConfig] = useState<{ execution_mode: string; model: string } | null>(null);
  const [runs, setRuns] = useState<WorkflowRun[]>([]);
  const [selected, setSelected] = useState<WorkflowRun | null>(null);
  const [artifacts, setArtifacts] = useState<ArtifactRecord[]>([]);
  const [finopsByRun, setFinopsByRun] = useState<Record<string, FinOpsReport>>({});
  const [artifactContent, setArtifactContent] = useState<unknown | null>(null);
  const [artifactTitle, setArtifactTitle] = useState("");
  const [history, setHistory] = useState<WorkflowEvent[]>([]);
  const [tasks, setTasks] = useState<DelegatedTask[]>([]);
  const [interactions, setInteractions] = useState<HumanInteraction[]>([]);
  const [evaluation, setEvaluation] = useState<EvaluationView | null>(null);
  const [cancelRequested, setCancelRequested] = useState(false);
  const [retryRequested, setRetryRequested] = useState(false);
  const [retryGeneration, setRetryGeneration] = useState(0);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    void Promise.all([api.platform(), api.runs(), api.config(), api.sandboxes()])
      .then(([host, existingRuns, harness, qualification]) => {
        setSandboxes(qualification);
        setConfig(harness);
        setPlatform(host);
        setRuns(existingRuns);
        if (existingRuns[0]) setSelected(existingRuns[0]);
      })
      .catch((reason: Error) => setError(reason.message));
  }, []);

  useEffect(() => {
    if (!selected) return;
    setArtifactContent(null);
    setCancelRequested(false);
    setTasks([]);
    setInteractions([]);
    setEvaluation(null);
    let disposed = false;
    let refreshing = false;
    let refreshAgain = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let stream: EventSource | undefined;

    // Coalesce bursts of A2A events; serialized REST reads avoid stale-state races.
    const refresh = async () => {
      if (disposed) return;
      if (refreshing) { refreshAgain = true; return; }
      refreshing = true;
      try {
        do {
          refreshAgain = false;
          const [run, output, events, remoteTasks, humanRequests, evaluationView, finops] = await Promise.all([
            api.run(selected.run_id), api.artifacts(selected.run_id),
            api.history(selected.run_id), api.tasks(selected.run_id), api.interactions(selected.run_id),
            api.evaluation(selected.run_id), api.finops(selected.run_id),
          ]);
          if (disposed) return;
          setSelected(run);
          setArtifacts(output);
          setHistory(events);
          setTasks(remoteTasks);
          setInteractions(humanRequests);
          setEvaluation(evaluationView);
          setFinopsByRun((reports) => ({ ...reports, [run.run_id]: finops }));
          setRuns((items) => [run, ...items.filter((item) => item.run_id !== run.run_id)]);
          if (TERMINAL.has(run.status)) stream?.close();
        } while (refreshAgain && !disposed);
      } catch (reason) {
        if (!disposed) setError(reason instanceof Error ? reason.message : "Progress refresh failed");
      } finally {
        refreshing = false;
      }
    };
    void refresh();
    if (!TERMINAL.has(selected.status)) {
      stream = new EventSource(api.eventsUrl(selected.run_id));
      const scheduleRefresh = () => {
        clearTimeout(timer);
        timer = setTimeout(() => { void refresh(); }, 40);
      };
      [
        "stage.started", "stage.completed", "artifact.created",
        "agent.started", "agent.completed", "run.completed", "run.failed", "run.blocked",
        "run.canceled", "run.cancellation_requested", "a2a.agent_discovered",
        "a2a.task_status", "a2a.retry", "a2a.cancellation_result", "a2a.cancellation_failed",
        "human.requested", "human.responded", "agent.execution",
        "mcp.analysis", "sandbox.executed", "sandbox.qualified",
        "quality_gates.completed", "evaluation.decision", "repair.started", "repair.exhausted",
        "run.retry_requested", "stage.reused", "agent.reused",
      ].forEach((name) => stream!.addEventListener(name, scheduleRefresh));
      stream.onerror = scheduleRefresh;
    }
    return () => { disposed = true; clearTimeout(timer); stream?.close(); };
  }, [selected?.run_id, retryGeneration]);

  useEffect(() => {
    if (artifactContent === null) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setArtifactContent(null);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [artifactContent]);

  async function startRun(event: FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      const run = await api.createRun(idea);
      setRuns((items) => [run, ...items]);
      setSelected(run);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to start run");
    } finally {
      setSubmitting(false);
    }
  }

  async function cancelRun() {
    if (!selected) return;
    setCancelRequested(true);
    try {
      await api.cancel(selected.run_id);
    } catch (reason) {
      setCancelRequested(false);
      setError(reason instanceof Error ? reason.message : "Cancellation failed");
    }
  }

  async function retryRun() {
    if (!selected || retryRequested) return;
    setRetryRequested(true);
    setError("");
    try {
      const run = await api.retry(selected.run_id);
      setSelected(run);
      setRuns((items) => items.map((item) => item.run_id === run.run_id ? run : item));
      setRetryGeneration((value) => value + 1);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Retry failed");
    } finally {
      setRetryRequested(false);
    }
  }

  return (
    <main>
      <header>
        <div>
          <p className="eyebrow">AI-driven development lifecycle</p>
          <h1>Learning workbench</h1>
          <p className="lede">Turn one line into traceable MVP artifacts with interactive human gates.</p>
          <p className="protocol-note">A2A 1.0 · JSON-RPC · A2UI v0.9.1 · {config?.execution_mode ?? "Loading"} mode
            {config?.execution_mode === "deep" && ` · all spokes: ${config.model}; MCP + sandbox execution`}</p>
        </div>
        <span className={`host ${platform?.ready ? "pass" : "warn"}`}>
          {platform ? (platform.ready ? "Host ready" : "Host action needed") : "Checking host"}
        </span>
      </header>

      {error && <p className="error" role="alert">{error}</p>}

      {platform && (
        <details className="host-details">
          <summary>Host capabilities · {platform.os_id} {platform.os_version} · {platform.architecture}</summary>
          <ul>
            {platform.checks.map((check) => (
              <li key={check.name}>
                <strong>{check.status}</strong> {check.name}: {check.detail}
                {!check.required && " (optional)"}
              </li>
            ))}
          </ul>
        </details>
      )}

      {sandboxes && (
        <details className="host-details">
          <summary>Last sandbox qualification · {new Date(sandboxes.generated_at).toLocaleString()}</summary>
          <p>runc/runsc baseline: {sandboxes.baseline_qualified ? "qualified" : "incomplete"} ·
            microVM: {sandboxes.microvm_qualified ? "qualified" : "unavailable or unqualified"}</p>
          <ul>
            {sandboxes.runtimes.map((runtime) => (
              <li key={runtime.runtime}>
                <strong>{runtime.status}</strong> {runtime.runtime}
                {runtime.jobs.probe.execution.reason && `: ${runtime.jobs.probe.execution.reason}`}
                {runtime.status !== "unavailable" && ` · ${Object.values(runtime.jobs)
                  .reduce((sum, job) => sum + job.execution.duration_ms, 0)} ms total`}
                {runtime.timings && ` · build median ${runtime.timings.build.median_ms} ms ·
                  test median ${runtime.timings.test.median_ms} ms`}
                {runtime.relative_to_runc?.test != null &&
                  ` · test ${runtime.relative_to_runc.test.toFixed(2)}× runc`}
              </li>
            ))}
          </ul>
          {sandboxes.comparison && (
            <p>{sandboxes.comparison.repetitions} repetitions per available runtime;
              durations include container creation and cleanup.
              {!sandboxes.comparison.same_environment &&
                " Image, kernel, or policy differed; timing ratios are withheld."}</p>
          )}
        </details>
      )}

      <section className="start-card">
        <form onSubmit={startRun}>
          <label htmlFor="idea">Describe the product in one line</label>
          <div className="input-row">
            <input
              id="idea"
              value={idea}
              minLength={3}
              onChange={(event) => setIdea(event.target.value)}
            />
            <button disabled={submitting}>{submitting ? "Starting…" : "Start AIDLC"}</button>
          </div>
        </form>
      </section>

      <div className="workspace">
        <aside>
          <h2>Runs</h2>
          {runs.length === 0 && <p className="muted">No workflow runs yet.</p>}
          {runs.map((run) => (
            <button
              className={`run-item ${selected?.run_id === run.run_id ? "selected" : ""}`}
              key={run.run_id}
              onClick={() => setSelected(run)}
            >
              <span>{run.idea}</span>
              <small>{run.status}{finopsByRun[run.run_id]?.totals.actual_total_tokens != null &&
                ` · ${finopsByRun[run.run_id].totals.actual_total_tokens!.toLocaleString()} tokens`}</small>
            </button>
          ))}
        </aside>

        <section className="detail">
          {!selected ? (
            <p className="empty">Start or select a run to inspect its lifecycle.</p>
          ) : (
            <>
              <div className="detail-title">
                <div><p className="eyebrow">{selected.project_id}</p><h2>{selected.idea}</h2></div>
                <span className={`status ${selected.status}`}>{selected.status}</span>
                {["failed", "blocked", "canceled"].includes(selected.status) && (
                  <button disabled={retryRequested} onClick={() => { void retryRun(); }}>
                    {retryRequested ? "Retrying…" : "Retry failed stage"}
                  </button>
                )}
                {!TERMINAL.has(selected.status) && (
                  <button className="cancel-button" disabled={cancelRequested} onClick={() => { void cancelRun(); }}>
                    {cancelRequested ? "Cancel requested…" : "Cancel run"}
                  </button>
                )}
              </div>
              {selected.error && <p className="error">{selected.error}</p>}
              <div className="project-usage" aria-label="Project model usage">
                <div><small>Project tokens</small><strong>
                  {finopsByRun[selected.run_id]?.totals.actual_total_tokens?.toLocaleString() ?? "Unavailable"}
                </strong></div>
                <div><small>Estimated cost</small><strong>
                  {finopsByRun[selected.run_id]?.totals.actual_cost_usd != null
                    ? `$${finopsByRun[selected.run_id].totals.actual_cost_usd!.toFixed(4)}`
                    : "Unavailable"}
                </strong></div>
              </div>
              {selected.status === "input_required" && interactions.filter((item) => !item.response &&
                [...tasks].reverse().find((task) => task.agent === item.agent && task.stage === item.stage)
                  ?.task_id === item.task_id).map(
                (item) => <HumanSurface key={item.surface_id} interaction={item} />
              )}
              <ol className="stages">
                {selected.stages.map((stage, index) => {
                  const phaseArtifacts = artifacts.filter((artifact) => artifact.metadata.stage_id === stage.name);
                  return (
                  <li key={stage.name} className={stage.status}>
                    <span className="stage-number">{index + 1}</span>
                    <span className="stage-name">{stage.name}</span>
                    <small>{stage.status}</small>
                    {["discovery", "implementation", "integration"].includes(stage.name) &&
                      <small>Parallel specialists</small>}
                    <div className="phase-artifacts">
                      {phaseArtifacts.map((artifact) => (
                        <button
                          className="artifact-link"
                          key={artifact.metadata.artifact_id}
                          onClick={() => {
                            setArtifactTitle(artifact.metadata.kind.replaceAll("_", " "));
                            void api.artifact(artifact.metadata.artifact_id)
                              .then((result) => setArtifactContent(result.content))
                              .catch((reason: Error) => setError(reason.message));
                          }}
                        >{artifact.metadata.kind.replaceAll("_", " ")}</button>
                      ))}
                      {phaseArtifacts.length === 0 && <small>No artifacts yet</small>}
                    </div>
                  </li>
                  );
                })}
              </ol>
              <details className="task-panel" open>
                <summary>A2A delegated tasks · {tasks.length}</summary>
                <div className="task-scroll">
                  <table>
                    <thead><tr><th>Agent / stage</th><th>State</th><th>Attempts</th><th>Task / context identity</th></tr></thead>
                    <tbody>
                      {tasks.map((task) => (
                        <tr key={task.invocation_id}>
                          <td>
                            <a href={task.endpoint.replace(/\/rpc$/, "/.well-known/agent-card.json")} target="_blank" rel="noreferrer">{task.agent}</a>
                            <small>{task.stage}{task.repair_attempt > 0 && ` · repair ${task.repair_attempt}`}</small>
                          </td>
                          <td>{task.state}</td><td>{task.attempts}</td>
                          <td><code>{task.task_id ?? "Awaiting remote task"}</code><small>{task.context_id}</small></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </details>
              <EvaluationPanel view={evaluation} />
              <details className="event-history">
                <summary>Agent and workflow event history · {history.length} events</summary>
                <ol>
                  {history.map((event) => (
                    <li key={event.event_id}>
                      <code>{event.event_type}</code>
                      <small>{JSON.stringify(event.payload)}</small>
                    </li>
                  ))}
                </ol>
              </details>
            </>
          )}
        </section>
      </div>

      {artifactContent !== null && (
        <div className="modal-backdrop" role="presentation" onMouseDown={() => setArtifactContent(null)}>
          <section
            className="artifact-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="artifact-modal-title"
            onMouseDown={(event) => event.stopPropagation()}
          >
            <div className="artifact-modal-header">
              <div>
                <p className="eyebrow">Artifact details</p>
                <h2 id="artifact-modal-title">{artifactTitle}</h2>
              </div>
              <button className="modal-close" aria-label="Close artifact details" onClick={() => setArtifactContent(null)}>
                Close
              </button>
            </div>
            <pre className="artifact-content">{JSON.stringify(artifactContent, null, 2)}</pre>
          </section>
        </div>
      )}

      <footer>
        Review immutable artifacts, human decisions, sandbox evidence, and evaluation before release.
      </footer>
    </main>
  );
}
