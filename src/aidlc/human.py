"""A small, server-owned A2UI v0.9.1 catalog. No model-authored HTML or URLs."""

from typing import Any

CATALOG_ID = "urn:aidlc:human-controls:v1"


def surface_messages(surface_id: str, question: str, kind: str) -> list[dict[str, Any]]:
    decisions = (
        ["submit"]
        if kind == "clarification"
        else ["accept", "repair"]
        if kind == "evaluation_decision"
        else ["approve", "reject"]
    )
    components: list[dict[str, Any]] = [
        {
            "id": "root",
            "component": "Column",
            "children": ["question", "answer", *[f"button-{item}" for item in decisions]],
        },
        {"id": "question", "component": "Text", "text": question, "variant": "caption"},
        {
            "id": "answer",
            "component": "TextField",
            "label": "Your answer" if kind == "clarification" else "Optional comment",
            "value": {"path": "/answer"},
        },
    ]
    for decision in decisions:
        components.extend(
            [
                {
                    "id": f"label-{decision}",
                    "component": "Text",
                    "text": decision.title(),
                    "variant": "caption",
                },
                {
                    "id": f"button-{decision}",
                    "component": "Button",
                    "child": f"label-{decision}",
                    "action": {
                        "event": {"name": decision, "context": {"answer": {"path": "/answer"}}}
                    },
                },
            ]
        )
    return [
        {"version": "v0.9.1", "createSurface": {"surfaceId": surface_id, "catalogId": CATALOG_ID}},
        {
            "version": "v0.9.1",
            "updateComponents": {"surfaceId": surface_id, "components": components},
        },
        {
            "version": "v0.9.1",
            "updateDataModel": {"surfaceId": surface_id, "path": "/", "value": {"answer": ""}},
        },
    ]
