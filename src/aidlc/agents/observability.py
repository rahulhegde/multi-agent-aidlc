"""Backend-owned model diagnostics, kept outside project artifact snapshots."""

import json
import logging
import os
import re
import threading
import time
import traceback
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from pydantic import BaseModel

from aidlc.config import Settings
from aidlc.domain.models import AgentContext, utc_now

LOGGER = logging.getLogger(__name__)
SECRET_KEYS = {
    "authorization",
    "api_key",
    "apikey",
    "password",
    "secret",
    "access_token",
    "refresh_token",
    "id_token",
    "client_secret",
    "encrypted_content",
    "headers",
}


def _redact(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, BaseModel):
        return _redact(value.model_dump(mode="json"), secrets)
    if isinstance(value, dict):
        return {
            str(key): "[redacted]" if str(key).lower() in SECRET_KEYS else _redact(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[redacted]")
        return re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]+|\bsk-[A-Za-z0-9_-]+", "[redacted]", value)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return _redact(str(value), secrets)


class ModelTrace(BaseCallbackHandler):
    """One append-only file per invocation; callbacks survive graph validation failures."""

    def __init__(self, settings: Settings, context: AgentContext, agent: str) -> None:
        super().__init__()
        self.execution_id = uuid4().hex
        self.path: Path | None = (
            settings.data_dir
            / "logs"
            / context.project_id
            / context.stage.value
            / agent
            / f"{self.execution_id}.jsonl"
            if settings.llm_logging_enabled
            else None
        )
        self.identity = {
            "project_id": context.project_id,
            "workflow_run_id": context.run_id,
            "execution_id": self.execution_id,
            "stage": context.stage.value,
            "agent": agent,
            "model": settings.model,
            "repair_attempt": context.repair_attempt,
            "retry_attempt": context.retry_attempt,
        }
        self._lock = threading.RLock()
        self._started = time.monotonic()
        self._calls: dict[str, float] = {}
        self.llm_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self._secrets = tuple(
            value
            for key, value in os.environ.items()
            if len(value) >= 8 and key.endswith(("API_KEY", "TOKEN", "SECRET", "PASSWORD"))
        )

    def record(self, event: str, **details: Any) -> None:
        if self.path is None:
            return
        try:
            with self._lock:
                record = _redact(
                    {
                        "timestamp": utc_now().isoformat(),
                        **self.identity,
                        "event": event,
                        **details,
                    },
                    self._secrets,
                )
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError, TypeError, ValueError:
            # Observability must not turn a successful invocation into a failed task.
            LOGGER.warning("Could not write model diagnostics to %s", self.path, exc_info=True)

    def on_chat_model_start(
        self, serialized: dict[str, Any], messages: list[list[BaseMessage]], **kwargs: Any
    ) -> None:
        callback_id = str(kwargs.get("run_id", ""))
        with self._lock:
            self.llm_calls += 1
            self._calls[callback_id] = time.monotonic()
            self.record(
                "llm.started",
                callback_run_id=callback_id,
                call_number=self.llm_calls,
                input_message_count=sum(len(batch) for batch in messages),
            )

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        callback_id = str(kwargs.get("run_id", ""))
        messages = [
            generation.message
            for batch in response.generations
            for generation in batch
            if isinstance(generation, ChatGeneration)
        ]
        with self._lock:
            for message in messages:
                if isinstance(message, AIMessage) and message.usage_metadata:
                    self.input_tokens += message.usage_metadata["input_tokens"]
                    self.output_tokens += message.usage_metadata["output_tokens"]
            started = self._calls.pop(callback_id, None)
            self.record(
                "llm.completed",
                callback_run_id=callback_id,
                duration_seconds=time.monotonic() - started if started is not None else None,
                messages=messages,
            )

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        callback_id = str(kwargs.get("run_id", ""))
        with self._lock:
            started = self._calls.pop(callback_id, None)
            self.record(
                "llm.failed",
                callback_run_id=callback_id,
                duration_seconds=time.monotonic() - started if started is not None else None,
                error_type=type(error).__name__,
                error=str(error),
            )

    def graph_result(self, output: dict[str, Any], attempt: int) -> None:
        self.record(
            "graph.completed",
            structured_output_attempt=attempt,
            structured_response=output.get("structured_response"),
            messages=[
                message
                for message in output.get("messages", [])
                if isinstance(message, (AIMessage, ToolMessage))
            ],
        )

    def finish(self, event: str, error: BaseException | None = None) -> None:
        self.record(
            event,
            duration_seconds=time.monotonic() - self._started,
            llm_calls=self.llm_calls,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            **(
                {
                    "error_type": type(error).__name__,
                    "error": str(error),
                    "traceback": "".join(traceback.format_exception(error)),
                }
                if error
                else {}
            ),
        )
