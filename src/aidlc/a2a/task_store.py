from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from a2a.server.context import ServerCallContext
from a2a.server.owner_resolver import resolve_user_scope
from a2a.server.tasks import TaskStore
from a2a.types import ListTasksRequest, ListTasksResponse, Message, Task, TaskState, TaskStatus
from a2a.utils.errors import InvalidParamsError
from a2a.utils.task import decode_page_token, encode_page_token
from google.protobuf.json_format import MessageToJson, Parse


class SQLiteTaskStore(TaskStore):
    """Durable SDK task storage and atomic message-id reservations.

    One fleet worker is supported. Namespace and owner prevent cross-agent
    task lookup; production identity enforcement is a later milestone.
    """

    def __init__(self, path: Path, agent: str) -> None:
        self.path = path
        self.agent = agent
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS tasks (
                    agent TEXT, owner TEXT, id TEXT, payload TEXT NOT NULL,
                    PRIMARY KEY(agent, owner, id)
                );
                CREATE TABLE IF NOT EXISTS messages (
                    agent TEXT, owner TEXT, message_id TEXT, fingerprint TEXT NOT NULL,
                    task_id TEXT NOT NULL, PRIMARY KEY(agent, owner, message_id)
                );
                """
            )
            rows = connection.execute(
                "SELECT owner, id, payload FROM tasks WHERE agent = ?", (agent,)
            ).fetchall()
            # A restart must not leave tasks eternally working or rerun them silently.
            for owner, task_id, payload in rows:
                task = Parse(payload, Task())
                if task.status.state in {
                    TaskState.TASK_STATE_SUBMITTED,
                    TaskState.TASK_STATE_WORKING,
                }:
                    task.status.state = TaskState.TASK_STATE_FAILED
                    task.status.message.CopyFrom(
                        Message(role="ROLE_AGENT", parts=[{"text": "Agent process restarted"}])
                    )
                    connection.execute(
                        "UPDATE tasks SET payload = ? WHERE agent = ? AND owner = ? AND id = ?",
                        (MessageToJson(task), agent, owner, task_id),
                    )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _owner(context: ServerCallContext) -> str:
        return f"{context.tenant}:{resolve_user_scope(context)}"

    def reserve(self, message: Message, context: ServerCallContext) -> tuple[Task, bool]:
        owner = self._owner(context)
        fingerprint = hashlib.sha256(message.SerializeToString(deterministic=True)).hexdigest()
        with self._connect() as connection:
            # Reserve before starting execution, including before the first streamed event.
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT fingerprint, task_id FROM messages "
                "WHERE agent = ? AND owner = ? AND message_id = ?",
                (self.agent, owner, message.message_id),
            ).fetchone()
            if row:
                if row[0] != fingerprint:
                    raise InvalidParamsError("messageId reused with different input")
                payload = connection.execute(
                    "SELECT payload FROM tasks WHERE agent = ? AND owner = ? AND id = ?",
                    (self.agent, owner, row[1]),
                ).fetchone()
                if payload is None:
                    raise InvalidParamsError("The reserved task was deleted")
                return Parse(payload[0], Task()), False
            if message.task_id:
                row = connection.execute(
                    "SELECT payload FROM tasks WHERE agent = ? AND owner = ? AND id = ?",
                    (self.agent, owner, message.task_id),
                ).fetchone()
                if row is None:
                    raise InvalidParamsError("Unknown continuation task")
                task = Parse(row[0], Task())
                if task.context_id != message.context_id:
                    raise InvalidParamsError("Continuation context mismatch")
                if task.status.state != TaskState.TASK_STATE_INPUT_REQUIRED:
                    raise InvalidParamsError("Task is not waiting for input")
                task.history.append(message)
                task.status.state = TaskState.TASK_STATE_SUBMITTED
                connection.execute(
                    "UPDATE tasks SET payload = ? WHERE agent = ? AND owner = ? AND id = ?",
                    (MessageToJson(task), self.agent, owner, task.id),
                )
            else:
                task = Task(
                    id=str(uuid4()),
                    context_id=message.context_id or str(uuid4()),
                    status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
                    history=[message],
                )
                connection.execute(
                    "INSERT INTO tasks VALUES (?, ?, ?, ?)",
                    (self.agent, owner, task.id, MessageToJson(task)),
                )
            connection.execute(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?)",
                (self.agent, owner, message.message_id, fingerprint, task.id),
            )
            return task, True

    async def save(self, task: Task, context: ServerCallContext) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO tasks VALUES (?, ?, ?, ?) ON CONFLICT(agent, owner, id) "
                "DO UPDATE SET payload = excluded.payload",
                (self.agent, self._owner(context), task.id, MessageToJson(task)),
            )

    async def get(self, task_id: str, context: ServerCallContext) -> Task | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM tasks WHERE agent = ? AND owner = ? AND id = ?",
                (self.agent, self._owner(context), task_id),
            ).fetchone()
        return Parse(row[0], Task()) if row else None

    async def delete(self, task_id: str, context: ServerCallContext) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM tasks WHERE agent = ? AND owner = ? AND id = ?",
                (self.agent, self._owner(context), task_id),
            )

    async def list(self, params: ListTasksRequest, context: ServerCallContext) -> ListTasksResponse:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM tasks WHERE agent = ? AND owner = ?",
                (self.agent, self._owner(context)),
            ).fetchall()
        tasks = [Parse(row[0], Task()) for row in rows]
        tasks = [
            task
            for task in tasks
            if (not params.context_id or task.context_id == params.context_id)
            and (not params.status or task.status.state == params.status)
            and (
                not params.HasField("status_timestamp_after")
                or task.status.timestamp.ToJsonString()
                >= params.status_timestamp_after.ToJsonString()
            )
        ]
        tasks.sort(key=lambda task: (task.status.timestamp.ToJsonString(), task.id), reverse=True)
        total = len(tasks)
        if params.page_token:
            cursor = decode_page_token(params.page_token)
            index = next((i for i, task in enumerate(tasks) if task.id == cursor), None)
            if index is None:
                raise InvalidParamsError("Invalid page token")
            tasks = tasks[index:]
        size = params.page_size or 50
        return ListTasksResponse(
            tasks=tasks[:size],
            total_size=total,
            page_size=size,
            next_page_token=encode_page_token(tasks[size].id) if len(tasks) > size else "",
        )
