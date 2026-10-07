"""
Task Tool - Parallel Sub-Agent Execution for AnkaLoop.

This module provides task management capabilities that allow the main agent
to spawn sub-agents for parallel task execution, inspired by OpenCode's
task system.

Key Features:
- Create and manage parallel tasks (sub-agents)
- Track task status and results
- Support for task dependencies
- Cancellation support
- Event-driven task lifecycle
- Resource limits per task

Task States:
- PENDING: Task created but not started
- RUNNING: Task is executing
- COMPLETED: Task finished successfully
- FAILED: Task failed with an error
- CANCELLED: Task was cancelled

Example:
    from ankaloop.task import TaskManager

    manager = TaskManager()

    # Create a task
    task = await manager.create_task(
        description="Analyze the codebase structure",
        agent_type="explorer",
        parent_session_id="main-session",
    )

    # Wait for completion
    result = await manager.wait_for_task(task.id)
    print(result.result)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from .event_bus import (
    EventType,
    emit_task_event,
)
from .multi_agent import AgentMode, create_subagent_config, get_agent_registry
from .tracing import semconv as sc

logger = logging.getLogger(__name__)


class TaskState(Enum):
    """States a task can be in."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskPriority(Enum):
    """Priority levels for task execution."""

    LOW = 0
    NORMAL = 1
    HIGH = 2
    URGENT = 3


@dataclass(frozen=True)
class DelegationEnvelope:
    """Explicit, minimal context passed from a parent agent to a task child."""

    goal: str
    known_context: str | None = None
    relevant_files: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    validation_required: list[str] = field(default_factory=list)
    expected_return_shape: str | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DelegationEnvelope:
        """Build an envelope from model-supplied structured fields."""
        allowed_fields = {
            "goal",
            "known_context",
            "relevant_files",
            "constraints",
            "validation_required",
            "expected_return_shape",
        }
        unknown_fields = sorted(set(value) - allowed_fields)
        if unknown_fields:
            raise ValueError(f"unknown envelope fields: {', '.join(unknown_fields)}")
        goal = value.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("'envelope.goal' must be a non-empty string")
        return cls(
            goal=goal.strip(),
            known_context=_structured_optional_text(value, "known_context"),
            relevant_files=_structured_string_list(value, "relevant_files"),
            constraints=_structured_string_list(value, "constraints"),
            validation_required=_structured_string_list(value, "validation_required"),
            expected_return_shape=_structured_optional_text(value, "expected_return_shape"),
        )

    def render(self) -> str:
        """Render a stable user prompt without adding trusted runtime metadata."""
        sections = ["# Delegated task", "", "## Goal", self.goal]
        if self.known_context:
            sections.extend(["", "## Known context / evidence", self.known_context])
        _append_list_section(sections, "Relevant files", self.relevant_files)
        _append_list_section(sections, "Constraints / non-goals", self.constraints)
        _append_list_section(sections, "Validation required", self.validation_required)
        if self.expected_return_shape:
            sections.extend(["", "## Expected return shape", self.expected_return_shape])
        return "\n".join(sections)


def _optional_text(value: Any) -> str | None:
    """Normalize an optional envelope text field."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _structured_optional_text(value: dict[str, Any], field_name: str) -> str | None:
    """Validate and normalize one optional structured text field."""
    if field_name not in value or value[field_name] is None:
        return None
    field_value = value[field_name]
    if not isinstance(field_value, str):
        raise ValueError(f"'envelope.{field_name}' must be a string")
    return field_value.strip() or None


def _structured_string_list(value: dict[str, Any], field_name: str) -> list[str]:
    """Validate and normalize one optional structured string-list field."""
    if field_name not in value or value[field_name] is None:
        return []
    field_value = value[field_name]
    if not isinstance(field_value, list) or any(not isinstance(item, str) for item in field_value):
        raise ValueError(f"'envelope.{field_name}' must be an array of strings")
    return [item.strip() for item in field_value if item.strip()]


def _append_list_section(sections: list[str], title: str, items: list[str]) -> None:
    """Append a non-empty Markdown list section to an envelope prompt."""
    if items:
        sections.extend(["", f"## {title}", *(f"- {item}" for item in items)])


@dataclass
class Task:
    """A task that can be executed by a sub-agent.

    Attributes:
        id: Unique task identifier
        description: Human-readable task description
        envelope: Structured context rendered as the child user prompt
        agent_type: Type of agent to use (e.g., "explorer", "focused_coder")
        state: Current task state
        priority: Task priority
        parent_session_id: Parent session that created this task
        work_dir: Trusted working directory inherited from the parent agent
        created_at: When the task was created
        started_at: When execution started
        completed_at: When execution completed
        result: Task result (if completed)
        error: Error message (if failed)
        metadata: Additional task metadata
    """

    id: str
    description: str
    agent_type: str
    envelope: DelegationEnvelope | None = field(default=None, kw_only=True)
    state: TaskState = TaskState.PENDING
    priority: TaskPriority = TaskPriority.NORMAL
    parent_session_id: str | None = None
    work_dir: Path | None = None
    created_at: datetime = field(default_factory=datetime.now)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    _future: asyncio.Future | None = field(default=None, repr=False)
    _task: asyncio.Task | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Map legacy direct construction to a goal-only delegation envelope."""
        self.envelope = self.envelope or DelegationEnvelope(goal=self.description)
        self.description = self.envelope.goal

    @classmethod
    def create(
        cls,
        description: str | None = None,
        agent_type: str = "focused_coder",
        priority: TaskPriority = TaskPriority.NORMAL,
        parent_session_id: str | None = None,
        work_dir: Path | None = None,
        envelope: DelegationEnvelope | None = None,
        **metadata: Any,
    ) -> Task:
        """Create a new task.

        Args:
            description: Task description
            agent_type: Type of agent to use
            priority: Task priority
            parent_session_id: Parent session ID
            work_dir: Trusted working directory inherited from the parent agent
            envelope: Structured delegation context; description maps to its goal by default
            **metadata: Additional metadata

        Returns:
            New Task instance
        """
        resolved_description = _optional_text(description)
        if envelope is None and not resolved_description:
            raise ValueError("description or envelope goal is required")
        resolved_envelope = envelope or DelegationEnvelope(goal=resolved_description or "")
        return cls(
            id=str(uuid.uuid4())[:8],
            description=resolved_envelope.goal,
            envelope=resolved_envelope,
            agent_type=agent_type,
            priority=priority,
            parent_session_id=parent_session_id,
            work_dir=work_dir.expanduser().resolve() if work_dir else None,
            metadata=metadata,
        )

    @property
    def is_done(self) -> bool:
        """Check if task is in a terminal state."""
        return self.state in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED)

    @property
    def duration_ms(self) -> float | None:
        """Get task duration in milliseconds."""
        if self.started_at is None:
            return None
        end_time = self.completed_at or datetime.now()
        return (end_time - self.started_at).total_seconds() * 1000

    def to_dict(self) -> dict[str, Any]:
        """Convert task to dictionary representation."""
        envelope = self.envelope
        assert envelope is not None
        return {
            "id": self.id,
            "description": self.description,
            "envelope": {
                "goal": envelope.goal,
                "known_context": envelope.known_context,
                "relevant_files": envelope.relevant_files,
                "constraints": envelope.constraints,
                "validation_required": envelope.validation_required,
                "expected_return_shape": envelope.expected_return_shape,
            },
            "agent_type": self.agent_type,
            "state": self.state.value,
            "priority": self.priority.value,
            "parent_session_id": self.parent_session_id,
            "work_dir": str(self.work_dir) if self.work_dir else None,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "result": self.result,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "metadata": self.metadata,
        }


class TaskManager:
    """Manages task creation, execution, and lifecycle.

    The TaskManager allows spawning sub-agents to execute tasks in parallel,
    with support for priorities, cancellation, and status tracking.

    Example:
        manager = TaskManager()

        # Create multiple tasks
        task1 = await manager.create_task("Find all TODO comments", "explorer")
        task2 = await manager.create_task("Analyze dependencies", "planner")

        # Wait for all tasks
        results = await manager.wait_for_all([task1.id, task2.id])

        # Or wait for any task
        first_result = await manager.wait_for_any([task1.id, task2.id])
    """

    def __init__(self, max_concurrent: int = 5, max_terminal_tasks: int = 200):
        """Initialize the task manager.

        Args:
            max_concurrent: Maximum number of concurrent tasks
            max_terminal_tasks: Maximum completed, failed, or cancelled tasks retained
        """
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        if max_terminal_tasks < 0:
            raise ValueError("max_terminal_tasks must be non-negative")
        self._tasks: dict[str, Task] = {}
        self._max_concurrent = max_concurrent
        self._max_terminal_tasks = max_terminal_tasks
        self._terminal_task_ids: deque[str] = deque()
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._lock = asyncio.Lock()

    async def create_task(
        self,
        description: str | None = None,
        agent_type: str = "focused_coder",
        priority: TaskPriority = TaskPriority.NORMAL,
        parent_session_id: str | None = None,
        work_dir: Path | None = None,
        auto_start: bool = True,
        envelope: DelegationEnvelope | None = None,
        **metadata: Any,
    ) -> Task:
        """Create a new task.

        Args:
            description: Task description
            agent_type: Type of agent to use
            priority: Task priority
            parent_session_id: Parent session ID
            work_dir: Trusted working directory inherited from the parent agent
            auto_start: Whether to start the task immediately
            envelope: Structured delegation context; description remains backward compatible
            **metadata: Additional metadata

        Returns:
            The created Task
        """
        resolved_description = _optional_text(description)
        if envelope is None and not resolved_description:
            raise ValueError("description or envelope goal is required")

        # Validate agent type
        registry = get_agent_registry()
        if agent_type not in registry.list_agents():
            available = ", ".join(registry.list_agents())
            raise ValueError(f"Unknown agent type: {agent_type}. Available: {available}")

        # Create task
        task = Task.create(
            description=resolved_description,
            agent_type=agent_type,
            priority=priority,
            parent_session_id=parent_session_id,
            work_dir=work_dir,
            envelope=envelope,
            **metadata,
        )

        async with self._lock:
            self._tasks[task.id] = task

        # Emit event
        await emit_task_event(
            EventType.TASK_CREATED,
            task.id,
            task.description,
            parent_session_id,
            agent_type=agent_type,
            priority=priority.value,
        )

        logger.info(f"Created task {task.id}: {task.description[:50]}...")

        if auto_start:
            await self.start_task(task.id)

        return task

    async def start_task(self, task_id: str) -> None:
        """Start executing a task.

        Args:
            task_id: ID of the task to start
        """
        task = self._tasks.get(task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")

        if task.state != TaskState.PENDING:
            raise ValueError(f"Task {task_id} is not pending (state: {task.state})")

        # Create future for result
        loop = asyncio.get_event_loop()
        task._future = loop.create_future()

        # Start execution in background
        task._task = asyncio.create_task(self._execute_task(task))

        logger.info(f"Started task {task_id}")

    async def _execute_task(self, task: Task) -> None:
        """Execute a task using a sub-agent.

        Args:
            task: The task to execute
        """
        try:
            # Cancellation must cover semaphore acquisition as well as agent
            # execution so a cancelled pending task can never start later.
            async with self._semaphore:
                if task.state != TaskState.PENDING:
                    return
                self._mark_running(task)

                await emit_task_event(
                    EventType.TASK_STARTED,
                    task.id,
                    task.description,
                    task.parent_session_id,
                )

                # Import here to avoid circular imports
                from .agent import create_agent_from_config

                # Get agent config
                registry = get_agent_registry()
                config = registry.get(task.agent_type)

                if config is None:
                    # Create a custom subagent config
                    config = create_subagent_config(
                        parent_name="task_manager",
                        task_description="Complete the delegated task in the user prompt.",
                    )

                # Ensure it's a subagent
                if config.mode == AgentMode.PRIMARY:
                    # Use focused_coder for primary agents to limit scope
                    config = registry.get("focused_coder") or config

                # Create agent
                agent = create_agent_from_config(config, ephemeral=True)
                # The subagent's turn span joins the parent's execute_tool span:
                # asyncio tasks inherit contextvars, so only descriptive
                # attributes need passing explicitly.
                execution_context = getattr(agent, "execution_context", None)
                if isinstance(execution_context, dict):
                    execution_context["source"] = "task"
                    execution_context["trace_attributes"] = {
                        sc.ANKALOOP_TASK_ID: task.id,
                        sc.ANKALOOP_TASK_AGENT_TYPE: task.agent_type,
                        sc.ANKALOOP_TASK_PARENT_SESSION_ID: task.parent_session_id,
                    }

                try:
                    # Execute the task in the same trusted workspace as its parent.
                    # Agent.run routes through the shared ApplicationSessionService.
                    envelope = task.envelope
                    assert envelope is not None
                    result = await agent.run(
                        user_input=envelope.render(),
                        work_dir=task.work_dir,
                        stream=False,
                        show_progress=False,
                    )
                finally:
                    await self._close_agent(agent)

                # Mark completed
                self._mark_completed(task, result)
                try:
                    await emit_task_event(
                        EventType.TASK_COMPLETED,
                        task.id,
                        task.description,
                        task.parent_session_id,
                        result=result[:500] if result else None,
                        duration_ms=task.duration_ms,
                    )
                finally:
                    self._finish_task(task)

                logger.info(f"Completed task {task.id} in {task.duration_ms:.0f}ms")

        except asyncio.CancelledError:
            if not task.is_done:
                self._mark_cancelled(task, "Task was cancelled")
                try:
                    await emit_task_event(
                        EventType.TASK_CANCELLED,
                        task.id,
                        task.description,
                        task.parent_session_id,
                    )
                finally:
                    self._finish_task(task)

                logger.info(f"Cancelled task {task.id}")

        except Exception as e:
            if not task.is_done:
                self._mark_failed(task, str(e))
                try:
                    await emit_task_event(
                        EventType.TASK_FAILED,
                        task.id,
                        task.description,
                        task.parent_session_id,
                        error=str(e),
                    )
                finally:
                    self._finish_task(task)

                logger.error(f"Task {task.id} failed: {e}")

    @staticmethod
    async def _close_agent(agent: Any) -> None:
        """Settle sub-agent cleanup before propagating cancellation."""
        session_id = getattr(agent, "session_id", "unknown")
        close_task = asyncio.create_task(agent.close(), name=f"ankaloop-close-agent-{session_id}")
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError as cancelled:
            while not close_task.done():
                try:
                    await asyncio.shield(close_task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not close_task.cancelled():
                with contextlib.suppress(Exception):
                    close_task.result()
            raise cancelled

    @staticmethod
    def _mark_running(task: Task) -> None:
        """Set task to running state."""
        task.state = TaskState.RUNNING
        task.started_at = datetime.now()

    @staticmethod
    def _mark_completed(task: Task, result: str) -> None:
        """Set task to completed state."""
        task.state = TaskState.COMPLETED
        task.completed_at = datetime.now()
        task.result = result

    @staticmethod
    def _mark_cancelled(task: Task, error: str) -> None:
        """Set task to cancelled state."""
        task.state = TaskState.CANCELLED
        task.completed_at = datetime.now()
        task.error = error

    @staticmethod
    def _mark_failed(task: Task, error: str) -> None:
        """Set task to failed state."""
        task.state = TaskState.FAILED
        task.completed_at = datetime.now()
        task.error = error

    def _finish_task(self, task: Task) -> None:
        """Resolve terminal completion and retain only a bounded history."""
        if task._future and not task._future.done():
            task._future.set_result(task)
        self._terminal_task_ids.append(task.id)
        while len(self._terminal_task_ids) > self._max_terminal_tasks:
            task_id = self._terminal_task_ids.popleft()
            retained = self._tasks.get(task_id)
            if retained is not None and retained.is_done:
                self._tasks.pop(task_id, None)

    async def cancel_task(self, task_id: str) -> bool:
        """Cancel a running task.

        Args:
            task_id: ID of the task to cancel

        Returns:
            True if task was cancelled
        """
        task = self._tasks.get(task_id)
        if task is None:
            return False

        if task.is_done:
            return False

        worker = task._task
        self._mark_cancelled(task, "Task was cancelled")

        if worker is not None and not worker.done():
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker

        try:
            await emit_task_event(
                EventType.TASK_CANCELLED,
                task.id,
                task.description,
                task.parent_session_id,
            )
        finally:
            self._finish_task(task)
        logger.info(f"Cancelled task {task.id}")
        return True

    async def cancel_for_session(self, session_id: str) -> int:
        """Cancel all pending or running tasks owned by a parent session."""
        cancelled = 0
        for task in list(self._tasks.values()):
            if task.parent_session_id != session_id or task.is_done:
                continue
            if await self.cancel_task(task.id):
                cancelled += 1
        return cancelled

    async def wait_for_task(self, task_id: str, timeout: float | None = None) -> Task:
        """Wait for a task to complete.

        Args:
            task_id: ID of the task to wait for
            timeout: Maximum time to wait (None = infinite)

        Returns:
            The completed task

        Raises:
            ValueError: If task not found
            asyncio.TimeoutError: If timeout exceeded
        """
        task = self._tasks.get(task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")

        if task._future is None:
            if task.is_done:
                return task
            raise ValueError(f"Task {task_id} was never started")

        if timeout is not None:
            await asyncio.wait_for(asyncio.shield(task._future), timeout)
        else:
            await asyncio.shield(task._future)

        return task

    async def wait_for_all(
        self,
        task_ids: list[str],
        timeout: float | None = None,
    ) -> list[Task]:
        """Wait for multiple tasks to complete.

        Args:
            task_ids: IDs of tasks to wait for
            timeout: Maximum time to wait

        Returns:
            List of completed tasks
        """
        tasks = []
        futures = []
        for task_id in task_ids:
            task = self._tasks.get(task_id)
            if task is None:
                raise ValueError(f"Task not found: {task_id}")
            tasks.append(task)
            if task._future:
                futures.append(task._future)

        if futures:
            completion = asyncio.gather(*(asyncio.shield(future) for future in futures))
            if timeout is not None:
                await asyncio.wait_for(completion, timeout)
            else:
                await completion

        return tasks

    async def wait_for_any(
        self,
        task_ids: list[str],
        timeout: float | None = None,
    ) -> Task:
        """Wait for any task to complete.

        Args:
            task_ids: IDs of tasks to wait for
            timeout: Maximum time to wait

        Returns:
            The first completed task
        """
        futures = []
        future_to_task: dict[asyncio.Future, Task] = {}

        for task_id in task_ids:
            task = self._tasks.get(task_id)
            if task is None:
                raise ValueError(f"Task not found: {task_id}")
            if task._future:
                futures.append(task._future)
                future_to_task[task._future] = task

        if not futures:
            raise ValueError("No running tasks to wait for")

        done, _ = await asyncio.wait(
            futures,
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )

        if not done:
            raise TimeoutError("No task completed within timeout")

        first_future = next(iter(done))
        return future_to_task[first_future]

    def get_task(self, task_id: str) -> Task | None:
        """Get a task by ID."""
        return self._tasks.get(task_id)

    def list_tasks(
        self,
        state: TaskState | None = None,
        parent_session_id: str | None = None,
    ) -> list[Task]:
        """List tasks with optional filtering.

        Args:
            state: Filter by state
            parent_session_id: Filter by parent session

        Returns:
            List of matching tasks
        """
        tasks = list(self._tasks.values())

        if state is not None:
            tasks = [t for t in tasks if t.state == state]

        if parent_session_id is not None:
            tasks = [t for t in tasks if t.parent_session_id == parent_session_id]

        return sorted(tasks, key=lambda t: t.created_at, reverse=True)

    def get_pending_count(self) -> int:
        """Get number of pending tasks."""
        return sum(1 for t in self._tasks.values() if t.state == TaskState.PENDING)

    def get_running_count(self) -> int:
        """Get number of running tasks."""
        return sum(1 for t in self._tasks.values() if t.state == TaskState.RUNNING)

    async def cleanup_completed(self, max_age_seconds: float = 3600) -> int:
        """Remove old completed tasks.

        Args:
            max_age_seconds: Maximum age of completed tasks to keep

        Returns:
            Number of tasks removed
        """
        now = datetime.now()
        to_remove = []

        async with self._lock:
            for task_id, task in self._tasks.items():
                if task.is_done and task.completed_at:
                    age = (now - task.completed_at).total_seconds()
                    if age > max_age_seconds:
                        to_remove.append(task_id)

            for task_id in to_remove:
                del self._tasks[task_id]
            if to_remove:
                removed = set(to_remove)
                self._terminal_task_ids = deque(
                    task_id for task_id in self._terminal_task_ids if task_id not in removed
                )

        return len(to_remove)

    def get_stats(self) -> dict[str, Any]:
        """Get task manager statistics."""
        by_state = {}
        for state in TaskState:
            by_state[state.value] = sum(1 for t in self._tasks.values() if t.state == state)

        return {
            "total_tasks": len(self._tasks),
            "by_state": by_state,
            "max_concurrent": self._max_concurrent,
            "available_slots": self._semaphore._value,
        }


# TaskTool implementation for agent use


class TaskTool:
    """Tool for agents to spawn and manage parallel sub-tasks.

    This tool allows agents to delegate work to sub-agents that run in parallel.

    Tool Schema:
        {
            "name": "task",
            "description": "Spawn and manage parallel sub-agent tasks",
            "parameters": {
                "action": "create|status|wait|cancel|list",
                "description": "Task description (for create)",
                "agent_type": "Agent type to use (for create)",
                "task_id": "Task ID (for status/wait/cancel)",
                "timeout": "Timeout in seconds (for wait)"
            }
        }
    """

    name = "task"
    description = """Spawn and manage parallel sub-agent tasks.

Actions:
- create: Create a new task for a sub-agent to execute
- status: Check the status of a task
- wait: Wait for a task to complete
- cancel: Cancel a running task
- list: List all tasks

Agent Types:
- explorer: Read-only codebase exploration (fast)
- planner: Analysis and planning (read-only)
- focused_coder: Specific implementation tasks

Example:
    {"action": "create", "description": "Find all TODO comments", "agent_type": "explorer"}
    {"action": "status", "task_id": "abc123"}
    {"action": "wait", "task_id": "abc123", "timeout": 60}
"""

    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "status", "wait", "cancel", "list"],
                "description": "Action to perform",
            },
            "description": {
                "type": "string",
                "description": "Legacy task description for create; maps to envelope.goal",
            },
            "envelope": {
                "type": "object",
                "description": "Explicit context for the delegated child (preferred for create)",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "Goal and expected outcome",
                    },
                    "known_context": {
                        "type": "string",
                        "description": "Relevant known context or evidence only",
                    },
                    "relevant_files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Files the child should inspect or change",
                    },
                    "constraints": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Constraints and explicit non-goals",
                    },
                    "validation_required": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Checks the child must run",
                    },
                    "expected_return_shape": {
                        "type": "string",
                        "description": "Required shape of the child's result",
                    },
                },
                "required": ["goal"],
                "additionalProperties": False,
            },
            "agent_type": {
                "type": "string",
                "enum": ["explorer", "planner", "focused_coder"],
                "description": "Agent type for the task (default: focused_coder)",
            },
            "task_id": {
                "type": "string",
                "description": "Task ID (required for status/wait/cancel)",
            },
            "timeout": {
                "type": "number",
                "description": "Timeout in seconds for wait action",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        manager: TaskManager,
        session_id: str | None = None,
        work_dir: Path | None = None,
    ):
        """Initialize TaskTool.

        Args:
            manager: Task manager owned by the parent agent runtime
            session_id: Session ID for tracking parent tasks
            work_dir: Trusted working directory inherited from the parent agent
        """
        self.session_id = session_id
        self.work_dir = work_dir.expanduser().resolve() if work_dir else None
        self._manager = manager

    async def execute(self, **kwargs: Any) -> str:
        """Execute the task tool.

        Args:
            **kwargs: Tool arguments

        Returns:
            Result as a string
        """
        action = kwargs.get("action")

        if action == "create":
            return await self._create_task(**kwargs)
        elif action == "status":
            return await self._get_status(**kwargs)
        elif action == "wait":
            return await self._wait_for_task(**kwargs)
        elif action == "cancel":
            return await self._cancel_task(**kwargs)
        elif action == "list":
            return await self._list_tasks(**kwargs)
        else:
            return f"Unknown action: {action}. Valid actions: create, status, wait, cancel, list"

    async def _create_task(self, **kwargs: Any) -> str:
        """Create a new task."""
        raw_envelope = kwargs.get("envelope")
        if raw_envelope is not None and not isinstance(raw_envelope, dict):
            return "Error: 'envelope' must be an object"
        try:
            envelope = DelegationEnvelope.from_dict(raw_envelope) if raw_envelope is not None else None
        except ValueError as exc:
            return f"Error: {exc}"
        description = _optional_text(kwargs.get("description"))
        if envelope is None and not description:
            return "Error: 'description' or 'envelope.goal' is required for create action"
        if envelope is None:
            envelope = DelegationEnvelope(goal=description or "")

        agent_type = kwargs.get("agent_type", "focused_coder")

        try:
            task = await self._manager.create_task(
                description=envelope.goal,
                agent_type=agent_type,
                parent_session_id=self.session_id,
                work_dir=self.work_dir,
                envelope=envelope,
            )
            return f"""Task created successfully!

Task ID: {task.id}
Description: {task.description}
Agent Type: {task.agent_type}
State: {task.state.value}

Use {{"action": "wait", "task_id": "{task.id}"}} to wait for results.
Use {{"action": "status", "task_id": "{task.id}"}} to check status."""

        except Exception as e:
            return f"Error creating task: {e}"

    async def _get_status(self, **kwargs: Any) -> str:
        """Get task status."""
        task_id = kwargs.get("task_id")
        if not task_id:
            return "Error: 'task_id' is required for status action"

        task = self._get_owned_task(task_id)
        if task is None:
            return f"Task not found: {task_id}"

        status_lines = [
            f"Task ID: {task.id}",
            f"Description: {task.description}",
            f"Agent Type: {task.agent_type}",
            f"State: {task.state.value}",
            f"Created: {task.created_at.isoformat()}",
        ]

        if task.started_at:
            status_lines.append(f"Started: {task.started_at.isoformat()}")

        if task.completed_at:
            status_lines.append(f"Completed: {task.completed_at.isoformat()}")

        if task.duration_ms:
            status_lines.append(f"Duration: {task.duration_ms:.0f}ms")

        if task.result:
            # Truncate long results
            result_preview = task.result[:500] + "..." if len(task.result) > 500 else task.result
            status_lines.append(f"\nResult:\n{result_preview}")

        if task.error:
            status_lines.append(f"\nError: {task.error}")

        return "\n".join(status_lines)

    async def _wait_for_task(self, **kwargs: Any) -> str:
        """Wait for a task to complete."""
        task_id = kwargs.get("task_id")
        if not task_id:
            return "Error: 'task_id' is required for wait action"

        if self._get_owned_task(task_id) is None:
            return f"Task not found: {task_id}"

        timeout = kwargs.get("timeout")

        try:
            task = await self._manager.wait_for_task(task_id, timeout)

            if task.state == TaskState.COMPLETED:
                return f"""Task {task.id} completed successfully!

Duration: {task.duration_ms:.0f}ms

Result:
{task.result}"""
            elif task.state == TaskState.FAILED:
                return f"Task {task.id} failed: {task.error}"
            elif task.state == TaskState.CANCELLED:
                return f"Task {task.id} was cancelled"
            else:
                return f"Task {task.id} is {task.state.value}"

        except TimeoutError:
            return f"Timeout waiting for task {task_id}"
        except Exception as e:
            return f"Error waiting for task: {e}"

    async def _cancel_task(self, **kwargs: Any) -> str:
        """Cancel a task."""
        task_id = kwargs.get("task_id")
        if not task_id:
            return "Error: 'task_id' is required for cancel action"

        if self._get_owned_task(task_id) is None:
            return f"Task not found: {task_id}"

        success = await self._manager.cancel_task(task_id)
        if success:
            return f"Task {task_id} cancelled successfully"
        else:
            return f"Could not cancel task {task_id} (may not be running)"

    async def _list_tasks(self, **kwargs: Any) -> str:
        """List all tasks."""
        tasks = self._manager.list_tasks(parent_session_id=self.session_id)

        if not tasks:
            return "No tasks found."

        lines = ["Tasks:"]
        for task in tasks:
            state_emoji = {
                TaskState.PENDING: "⏳",
                TaskState.RUNNING: "🔄",
                TaskState.COMPLETED: "✅",
                TaskState.FAILED: "❌",
                TaskState.CANCELLED: "🚫",
            }.get(task.state, "❓")

            lines.append(f"\n{state_emoji} {task.id}: {task.description[:50]}...")
            lines.append(f"   State: {task.state.value} | Agent: {task.agent_type}")

        running = sum(task.state == TaskState.RUNNING for task in tasks)
        lines.append(f"\nTotal: {len(tasks)} | Running: {running}")

        return "\n".join(lines)

    def _get_owned_task(self, task_id: str) -> Task | None:
        """Return a task only when it belongs to this tool's parent session."""
        task = self._manager.get_task(task_id)
        if task is None or task.parent_session_id != self.session_id:
            return None
        return task


def get_task_tool_schema() -> dict[str, Any]:
    """Get the JSON schema for the task tool."""
    return {
        "type": "function",
        "function": {
            "name": TaskTool.name,
            "description": TaskTool.description,
            "parameters": TaskTool.parameters,
        },
    }
