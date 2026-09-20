import { test } from "node:test";
import assert from "node:assert/strict";
import { A2uiMessageSchema, Catalog, MessageProcessor } from "@a2ui/web_core/v0_9";
import { A2uiSurface, Button, Column, Text, TextField } from "@a2ui/react/v0_9";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { Window } from "happy-dom";

const browser = new Window();
globalThis.window = browser;
globalThis.document = browser.document;
globalThis.HTMLElement = browser.HTMLElement;
globalThis.CSSStyleSheet = browser.CSSStyleSheet;
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const catalog = new Catalog("urn:aidlc:human-controls:v1", [Column, Text, TextField, Button]);
const messages = [
  { version: "v0.9.1", createSurface: { surfaceId: "fixture", catalogId: catalog.id } },
  { version: "v0.9.1", updateComponents: { surfaceId: "fixture", components: [
    { id: "root", component: "Column", children: ["question", "answer", "button-submit"] },
    { id: "question", component: "Text", text: "Who is the primary user?", variant: "caption" },
    { id: "answer", component: "TextField", label: "Your answer", value: { path: "/answer" } },
    { id: "label-submit", component: "Text", text: "Submit", variant: "caption" },
    { id: "button-submit", component: "Button", child: "label-submit",
      action: { event: { name: "submit", context: { answer: { path: "/answer" } } } } },
  ] } },
  { version: "v0.9.1", updateDataModel: { surfaceId: "fixture", path: "/", value: { answer: "" } } },
];

test("v0.9.1 surface renders and dispatches form actions", async () => {
  let action;
  const processor = new MessageProcessor([catalog], (value) => { action = value; }, { version: "v0.9.1" });
  processor.processMessages(messages.map((message) => A2uiMessageSchema.parse(message)));
  const surface = processor.model.getSurface("fixture");
  assert.ok(surface);
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () => { root.render(createElement(A2uiSurface, { surface })); });
    assert.match(container.textContent, /Who is the primary user/);
    assert.ok(container.querySelector("input"));
    assert.match(container.textContent, /Submit/);
    await act(async () => { surface.dataModel.set("/answer", "Students"); });
    await act(async () => { container.querySelector("button").click(); });
    assert.equal(action.name, "submit");
    assert.equal(action.context.answer, "Students");
    assert.equal(action.surfaceId, "fixture");
    assert.equal(processor.version, "v0.9.1");
  } finally {
    await act(async () => { root.unmount(); });
    processor.model.dispose();
    container.remove();
  }
});

test("unknown catalog and malformed messages are rejected", () => {
  const processor = new MessageProcessor([catalog], undefined, { version: "v0.9.1" });
  assert.throws(() => processor.processMessages([
    { version: "v0.9.1", createSurface: { surfaceId: "bad", catalogId: "untrusted" } },
  ]));
  assert.equal(A2uiMessageSchema.safeParse({ version: "v0.9.1", createSurface: {} }).success, false);
});
