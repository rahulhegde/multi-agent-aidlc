import { useMemo, useRef, useState } from "react";
import { A2uiSurface, Button, Column, Text, TextField } from "@a2ui/react/v0_9";
import { A2uiMessageSchema, Catalog, MessageProcessor } from "@a2ui/web_core/v0_9";
import { api } from "./api";
import type { HumanInteraction } from "./types";

const catalog = new Catalog("urn:aidlc:human-controls:v1", [Column, Text, TextField, Button]);

export function HumanSurface({ interaction }: { interaction: HumanInteraction }) {
  const [error, setError] = useState("");
  const [sent, setSent] = useState(false);
  const [answer, setAnswer] = useState("");
  const [pending, setPending] = useState(false);
  const busy = useRef(false);
  // Stable across retries of the same action; never generate an ID per HTTP attempt.
  const [actionId] = useState(() => crypto.randomUUID());
  const submit = async (decision: string, value: string) => {
    if (busy.current) return;
    busy.current = true;
    setPending(true);
    try {
      await api.action(interaction, decision, value, actionId);
      setSent(true);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Action failed");
    } finally { busy.current = false; setPending(false); }
  };
  const result = useMemo(() => {
    try {
      const processor = new MessageProcessor([catalog], (action) => {
        void submit(action.name, String(action.context.answer ?? ""));
      }, { version: "v0.9.1" });
      // Validate the envelope AND restrict the catalog before any rendering.
      const messages = interaction.messages.map((message) => A2uiMessageSchema.parse(message));
      for (const message of messages) {
        if ("updateComponents" in message && message.updateComponents.components.some(
          (component) => !catalog.components.has(component.component))) throw new Error("Unsupported component");
        if ("createSurface" in message && message.createSurface.catalogId !== catalog.id)
          throw new Error("Unsupported catalog");
      }
      processor.processMessages(messages);
      return { surface: processor.model.getSurface(interaction.surface_id), error: "" };
    } catch (reason) {
      return { surface: undefined, error: String(reason) };
    }
  }, [interaction.surface_id]);
  if (sent || interaction.response) return <p>Response recorded.</p>;
  return <section className="human-card" aria-label="Human input required">
    <h3>{interaction.prompt.kind === "evaluation_decision"
      ? "Evaluation decision required"
      : interaction.prompt.kind === "approval" ? "Approval required" : "Clarification required"}</h3>
    <p className="muted">{interaction.agent} · A2A task {interaction.task_id}</p>
    <p>Artifact: <code>{interaction.artifact_id}</code> · version {interaction.version}</p>
    <fieldset disabled={pending}>
      {result.surface ? <A2uiSurface surface={result.surface} /> : <form onSubmit={(event) => {
        event.preventDefault(); void submit("submit", answer);
      }}>
        <p>{interaction.prompt.question}</p>
        <label>Answer or comment<input value={answer} onChange={(event) => setAnswer(event.target.value)} /></label>
        {interaction.prompt.kind === "clarification" ? <button>Submit</button>
          : interaction.prompt.kind === "evaluation_decision" ? <>
          <button type="button" onClick={() => { void submit("accept", answer); }}>Accept</button>
          <button type="button" onClick={() => { void submit("repair", answer); }}>Repair</button>
        </> : <>
          <button type="button" onClick={() => { void submit("approve", answer); }}>Approve</button>
          <button type="button" onClick={() => { void submit("reject", answer); }}>Reject</button>
        </>}
        <small>Accessible fallback: this surface is unsupported by the renderer.</small>
      </form>}
    </fieldset>
    {error && <p role="alert" className="error">{error}</p>}
  </section>;
}
