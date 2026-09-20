import type { EvaluationView } from "./types";

export function EvaluationPanel({ view }: { view: EvaluationView | null }) {
  if (!view || (!view.quality_gates && !view.agent_evaluation)) return null;
  const gates = view.quality_gates;
  const evaluation = view.agent_evaluation;
  const scores = evaluation ? [
    ["Requirement coverage", evaluation.requirement_coverage],
    ["MVP completeness", evaluation.mvp_completeness],
    ["Usability", evaluation.usability],
    ["Architecture", evaluation.architecture],
    ["Maintainability", evaluation.maintainability],
    ["Risk acceptance", evaluation.risk_acceptance],
  ] as const : [];
  return (
    <section className="evaluation-panel" aria-label="Evaluation evidence">
      <h3>Evaluation</h3>
      {view.decision && <p>Decision: <strong>{view.decision.decision}</strong>
        {view.decision.overall_score !== null && ` · Model score: ${view.decision.overall_score.toFixed(2)}`}
        {` · Repair attempts: ${view.repair_attempts}`}</p>}
      {gates && <>
        <h4>Deterministic gates · {gates.verdict}</h4>
        <p>{gates.profile} · Attempt {gates.repair_attempt}. Passing this profile requires further release checks.</p>
        <div className="task-table"><table>
          <thead><tr><th>Gate</th><th>Status</th><th>Evidence</th></tr></thead>
          <tbody>{gates.gates.map((gate) => <tr key={gate.name}>
            <td>{gate.name.replaceAll("_", " ")}</td><td>{gate.status.replaceAll("_", " ")}</td>
            <td>{gate.detail}<small>{gate.evidence_artifact_ids.join(", ")}</small></td>
          </tr>)}</tbody>
        </table></div>
        <details><summary>Remaining release checks</summary>
          <ul>{gates.deferred_checks.map((check) => <li key={check}>{check}</li>)}</ul>
        </details>
      </>}
      {evaluation && <>
        <h4>Agent judgment · {evaluation.status ?? "legacy report"}</h4>
        <p>{evaluation.summary}</p>
        <dl>{scores.map(([name, score]) => score && <div key={name}>
          <dt>{name}: {score.applicability === "not_applicable" || score.score === null
            ? "Not applicable"
            : score.score.toFixed(2)}</dt><dd>{score.rationale}
            <small>{score.evidence_artifact_ids.join(", ")}</small></dd>
        </div>)}</dl>
        {!!evaluation.recommended_repairs?.length && <ul>
          {evaluation.recommended_repairs.map((repair, index) => <li key={index}>{repair}</li>)}
        </ul>}
        <p>Quality gates and model scores are decision evidence; the recorded human verdict is final.</p>
      </>}
    </section>
  );
}
