# Agent goals and shared context

The runtime goal contracts are the source of truth and currently use goal version `aidlc-agent-goals-v4`. Each contract is incorporated into its agent's system prompt, and its version is recorded in execution metadata. The role sections below reflect those contracts and must change with them. These are reasoning instructions, not additional authorization or enforced validators. Existing schemas, source ownership checks, trusted execution wrappers, and hub gates enforce their respective boundaries.

All spoke prompts share a learning-project output policy: use 1–3 short representative items per list by default, one simple approach, brief summaries, and the fewest complete runnable files permitted by each role. Lists need not be exhaustive. Approved requirements, required schema fields, quality gates, all evaluation rubric scores, and human approvals still take precedence. Markdown should summarize the result without repeating JSON lists or source code. This policy is prompt guidance, not a hard list-length validator.

## Artifact communication: implemented behavior

The hub sends an `AgentContext` as an A2A JSON data part over HTTP/JSON-RPC, with SSE task/status/artifact events. Context includes the run ID, idea, stage, artifact metadata and JSON content, human response, and repair feedback. Each invocation receives a snapshot; parallel siblings do not automatically see each other’s in-flight output.

Each spoke returns exactly one structured A2A result artifact containing an `AgentResult`: artifact kind, JSON content, markdown, optional human prompt, and execution metadata. The current client rejects multiple result artifacts or parts. A single result can contain many files: implementation outputs use `SourceBundle.files`, each with a path and content. Reports may reference additional trusted-service evidence artifacts. A wire artifact, a persisted artifact, and a source file are distinct objects.

Implementation models return only `SourceOutput.files`: complete source text and
relative paths, without a content wrapper, markdown summary, or explanation. The
backend packages these files as `AgentResult.content.files` and generates a
filename-only markdown listing. Backend output contains exactly the mandated
single backend entry module; frontend and test output retain their required
source set. Existing source snapshots,
integration, and release bundles keep the same JSON content format.

The hub assigns a project ID to each new workflow and includes it in the run, agent context, and artifact metadata. All persisted processing outputs belong to this project folder:

```text
<artifact-root>/projects/<project-id>/
├── project.json
├── current/<stage>/<producer>/<output-kind>/
│   ├── manifest.json
│   ├── content.json
│   ├── content.md
│   └── source/                  # release bundle only
│       └── <files[].path>       # materialized source with original extensions
└── records/<stage>/<artifact-id>/
```

`project.json` records project identity, the original idea, and a reference/hash for the current intake scope. The intake `ProjectBrief` establishes target user, problem, assumptions, and MVP boundaries. Source files remain embedded in JSON during intermediate stages. The release handoff stores both the structured bundle and a `source/` tree that preserves every validated relative path from `files[].path`.

## Current snapshots and shared context

Spoke outputs and integrated source are current-state snapshots, not a version history. A repair replaces the same producer/output-kind location and removes the superseded database index entry. Parallel roles have separate locations. Each publication receives a fresh artifact ID and content hash so earlier evidence cannot silently apply to a new snapshot, even when the source text is unchanged. Superseded output IDs are no longer available through the artifact API. Historical lineage and interaction/event references may therefore identify outputs whose content is no longer retained.

Project-level file locks coordinate cooperating writers and readers across hub/service processes; each file is atomically replaced under the lock. Readers check snapshot identity and content integrity. Failed publication or a stale database record fails closed rather than returning mismatched content. An out-of-order publisher cannot index a superseded snapshot. Filesystem and SQLite updates are not a distributed transaction; interruptions may require a new workflow, as with existing restart recovery.

Replacing an upstream snapshot removes dependent current snapshots. Changes to implementation source invalidate integrated source, spoke build/test/analysis reviews, quality gates, evaluation, and release. Reintegrating source likewise requires fresh validation. Trusted execution and human-decision records retain separate identities in `records/`; these are evidence/accountability records, not spoke output versions. Gates still require current source ID/hash matches, so old evidence cannot qualify a changed source.

Each specialist receives a deterministic projection of the project's current snapshots, defined by its `AgentSpec` input contract. Required dependencies fail before delegation when absent. Optional current source lets an implementation agent see only its own prior output during repair. Integration reviewers receive the integrated source without the three component source artifacts; test fixtures that intentionally bypass trusted integration fall back deterministically to the latest component snapshots. Only intake and requirements receive the original idea. Repair feedback is sent only during a repair attempt.

The hub still sends selected artifact JSON inline. Each context selection records the ordered input artifact IDs, serialized selected and broad-baseline context sizes, context hash, contract version, and cache key. Provider-reported tokens and configured-rate cost are recorded separately from the deterministic byte comparison and exposed by `GET /api/runs/{run-id}/finops`. The resulting artifact uses exactly those selected IDs as its lineage, so parallel completion order cannot create sibling dependencies.

Repair feedback is transported once in `repair_feedback`; `repair_artifact_id` and `repair_artifact_sha256` retain provenance without also embedding the persisted repair artifact content. Cache keys cover the agent, model, prompt and context-contract versions, semantic context, repair attempt, and non-secret execution profile. A current output is reused only when its cache key, contract version, and exact ordered parent artifact IDs match. Intake remains non-cacheable because its A2A clarification can add human input inside the remote task.

Deep Agents `StateBackend` remains invocation-local virtual storage; spokes do not write directly to the host project folder. The trusted hub persists their results. Transient sandbox workspaces remain isolated, with their resulting evidence stored under the project. Reference-only artifact transport remains a future extension.

A new run creates a new project. Retrying a failed, blocked, or canceled run keeps
that run and project identity, validates current snapshots, reuses completed
stages, and retries unfinished agents with fresh task IDs. Successful parallel
outputs are saved individually and can be reused after a sibling fails. Work
that was never saved as a hub artifact must be generated again. Evaluation repair
attempts and reported token usage carry forward. Outstanding remote tasks must
be reconciled or canceled before retry work starts. Reopening a project in a
separate run and reference-only artifact transport remain
future extensions. Existing older artifacts remain readable in their original
layout; they are not automatically migrated or deleted.

## Hub responsibility

The deterministic hub schedules stages, sends context snapshots through A2A, validates responses, persists artifacts and lineage, routes human decisions, integrates source, evaluates trusted quality gates, and controls bounded repair and release progression. It does not replace specialist reasoning or fabricate execution evidence.

## intake-agent

Goal version: aidlc-agent-goals-v4

Objective: Turn the original idea and any human answer into a bounded project brief.

Inputs: Original idea, supplied human response, and any existing project brief.

Deliverables: ProjectBrief: idea, target_user, problem, assumptions, and mvp_goal; readable markdown.

Completion criteria and boundaries: Identify the user and problem, distinguish assumptions from facts, and define an achievable MVP. Ask at most one material clarification using the clarification field; use an existing answer.

Tools and execution ownership: LLM reasoning only; no source changes, execution, or MCP analysis is needed.

## requirements-agent

Goal version: aidlc-agent-goals-v4

Objective: Translate the brief into verifiable, internally consistent MVP requirements.

Inputs: Project brief, original idea, and supplied human decisions.

Deliverables: RequirementsSpec in the required schema, plus readable markdown.

Completion criteria and boundaries: Define observable acceptance criteria, scope, and constraints. Preserve the brief's goal; make unresolved assumptions explicit rather than silently expanding scope.

Tools and execution ownership: LLM reasoning only; no source changes or execution.

## architecture-agent

Goal version: aidlc-agent-goals-v4

Objective: Choose the smallest architecture that satisfies approved requirements and runtime constraints.

Inputs: Project brief, requirements specification, and relevant repair feedback.

Deliverables: ArchitectureDecision: decision, components, constraints, and trade_offs.

Completion criteria and boundaries: Explain component responsibilities, interfaces, and concrete alternatives/tradeoffs. Respect the standard-library Python backend and pinned React toolchain; avoid unsupported services.

Tools and execution ownership: LLM reasoning only; produce design, not code or execution evidence.

## ux-agent

Goal version: aidlc-agent-goals-v4

Objective: Define usable interface behavior for the required user journeys.

Inputs: Project brief and requirements specification.

Deliverables: UxSpecification: journey, screens, and accessibility.

Completion criteria and boundaries: Cover primary journeys, input validation, loading/error/empty/success behavior, and keyboard and accessible-label expectations. Tie screens to requirements without adding unapproved scope.

Tools and execution ownership: LLM reasoning only; do not implement UI source or claim browser validation.

## security-agent

Goal version: aidlc-agent-goals-v4

Objective: Identify project-specific risks and actionable controls for the proposed MVP.

Inputs: Project brief and requirements specification.

Deliverables: ThreatModel: assets, threats, and controls.

Completion criteria and boundaries: Relate threats to assets and trust boundaries, prioritize controls, and describe residual risks. Distinguish proposed controls from implemented or verified controls.

Tools and execution ownership: LLM threat modeling only; no vulnerability scanner is exposed to this role.

## test-planner-agent

Goal version: aidlc-agent-goals-v4

Objective: Turn acceptance criteria into a feasible validation strategy.

Inputs: Requirements specification and project brief.

Deliverables: TestPlan: levels, critical_cases, and requirement_coverage.

Completion criteria and boundaries: Map each acceptance criterion to concrete cases including negative/boundary behavior. Distinguish planned tests from executed tests; mark browser/integration checks as deferred where the current validation environment cannot run them.

Tools and execution ownership: LLM planning only; no test execution or source changes.

## planning-agent

Goal version: aidlc-agent-goals-v4

Objective: Produce an executable dependency-aware plan that reconciles discovery into shared contracts.

Inputs: Requirements, architecture, UX, threat model, and test plan.

Deliverables: ImplementationPlan: backend_tasks, frontend_tasks, test_tasks, shared_contracts, dependency_order, and completion_criteria.

Completion criteria and boundaries: Specify exact API routes, methods, payloads, response/error shapes, and ownership. Plan the backend entirely in the mandated single entry module, with no additional backend modules. Resolve design inconsistencies explicitly and define verifiable completion criteria. Parallel implementation roles must be able to work from this same plan.

Tools and execution ownership: LLM planning only; no implementation or execution.

## backend-agent

Goal version: aidlc-agent-goals-v4

Objective: Deliver complete backend source satisfying assigned requirements and shared API contracts.

Inputs: Approved requirements, architecture, implementation plan, current source, and repair feedback.

Deliverables: SourceOutput containing exactly the mandated backend entry module and its complete source text.

Completion criteria and boundaries: Implement routes, validation, errors, and business behavior with the standard library in exactly one backend module. Do not generate a package initializer or additional backend modules. Keep the backend runnable as a module at `127.0.0.1:8081` using a namespace package. Preserve shared contracts during repair; never modify frontend or test source.

Tools and execution ownership: LLM source generation. Artifact-bound analyze_code MCP is available only when input source exists; it analyzes that input, not newly drafted files. No host shell or sandbox execution.

## frontend-agent

Goal version: aidlc-agent-goals-v4

Objective: Deliver a complete React TypeScript UI implementing the approved journeys and API contracts.

Inputs: Requirements, UX, architecture, implementation plan, current source, and repair feedback.

Deliverables: SourceOutput containing complete frontend source, including the required HTML entry point, TypeScript React entry point, and TypeScript configuration.

Completion criteria and boundaries: Use the pinned toolchain and relative `/api` URLs. Implement required interaction and accessible feedback states. Do not supply custom Vite configuration or a lockfile; any package manifest must exactly match the trusted toolchain. Modify only frontend source.

Tools and execution ownership: LLM source generation; input-artifact MCP analysis if exposed. No host execution or browser testing. Trusted sandbox services perform the subsequent TypeScript and Vite build.

## test-agent

Goal version: aidlc-agent-goals-v4

Objective: Deliver meaningful backend tests that demonstrate acceptance behavior and detect regressions.

Inputs: Requirements, test plan, shared API/module contracts, available source, and repair feedback.

Deliverables: SourceOutput containing complete Python test source, including the required package initializer.

Completion criteria and boundaries: Use standard-library unittest and approved imports. Assert behavior, edge cases, and failures; avoid placeholder assertions. Respect parallel execution: use shared contracts when sibling source is not yet supplied. Modify only test source; frontend checks currently cover its build.

Tools and execution ownership: LLM test generation; input-artifact MCP analysis if exposed. Trusted validation executes tests after integration; this role must not claim its generated tests have passed.

## build-agent

Goal version: aidlc-agent-goals-v4

Objective: Explain whether the current integrated source builds, using actual trusted execution evidence.

Inputs: Current integrated source and repair_feedback.execution_evidence from the sandbox wrapper.

Deliverables: ExecutionReviewOutput plus markdown; the wrapper supplies authoritative status/report IDs.

Completion criteria and boundaries: Identify failed steps, affected files, and bounded repairs from actual reports. Explain Python compilation and React TypeScript/Vite results separately. Do not replace measured status or infer success from source inspection.

Tools and execution ownership: The trusted wrapper must run sandbox build before LLM review. No model-selected shell commands or dependency scripts; do not modify source or gate results.

## validation-agent

Goal version: aidlc-agent-goals-v4

Objective: Assess actual test and UI-build evidence against planned acceptance coverage.

Inputs: Requirements, test plan, current integrated source, and sandbox execution_evidence.

Deliverables: ExecutionReviewOutput plus markdown; the wrapper supplies authoritative status/report IDs.

Completion criteria and boundaries: Report executed test outcomes, failures, and unverified coverage. UI build success does not prove browser interactions, usability, or end-to-end behavior. Recommend targeted repairs without inventing test counts or overriding trusted reports.

Tools and execution ownership: The trusted wrapper must run sandbox tests and UI build before LLM review. No source changes or arbitrary execution; browser/integration testing is outside current scope.

## static-analysis-agent

Goal version: aidlc-agent-goals-v4

Objective: Explain authenticated MCP analysis findings for the current source and recommend repairs.

Inputs: Current source and execution_evidence returned by the authenticated MCP analysis wrapper.

Deliverables: ExecutionReviewOutput plus markdown; the wrapper retains authoritative MCP reports/status.

Completion criteria and boundaries: Identify concrete findings and affected files, distinguish tool failures from clean analysis, and explain the limited scope of the configured analyzer. Do not claim a security audit or alter measured findings.

Tools and execution ownership: The trusted wrapper must call MCP analysis before LLM review. This role reviews results; it cannot edit source, bypass authentication, or run arbitrary tools.

## evaluation-agent

Goal version: aidlc-agent-goals-v4

Objective: Judge MVP readiness against requirements and six rubrics using current source and evidence.

Inputs: Original idea, requirements/design, current source, quality gates, execution reports, and the current repair attempt.

Deliverables: EvaluationReport with six rubric judgments and evidence IDs, summary, recommended_repairs, and markdown.

Completion criteria and boundaries: Judge requirement_coverage, mvp_completeness, usability, architecture, maintainability, and risk_acceptance with concrete rationale and existing input artifact IDs. Applicability comes only from approved requirements; generated designs and source do not create new requirements. Applicable rubrics receive a 0-to-1 score. A rubric not requested or implied by the requirements is `not_applicable`, has a null score, cites the requirements artifact, and explains why. Exclude out-of-scope implementation features, such as an unrequested UI, from every score and mention them only as non-scoring observations or risks. The overall score averages only applicable rubrics. When evidence is insufficient return not_evaluated and null rubric judgments. Recommend bounded repairs and never override deterministic gates or claim deferred checks were executed.

Tools and execution ownership: LLM evidence review; artifact-bound analyze_code MCP is available for current source. No source changes, sandbox command selection, or authority to approve release.

## release-agent

Goal version: aidlc-agent-goals-v4

Objective: Prepare reproducible handoff notes for the exact source accepted by gates and evaluation.

Inputs: Latest integrated source, quality gates, evaluation, and any supplied human decision.

Deliverables: ReleaseNotes: run_instructions, architecture_summary, known_limitations, and markdown; the trusted release assembler adds exact source files and evidence references.

Completion criteria and boundaries: Describe backend/UI startup and trusted dependencies accurately. State deferred checks and known limitations. Without explicit human acceptance, release assembly requires passing gates and the configured evaluation threshold; do not regenerate source, fabricate acceptance, or claim deployment.

Tools and execution ownership: LLM handoff writing only. Trusted code assembles source and pinned assets and enforces release eligibility; no deployment or external publication tools are exposed.
