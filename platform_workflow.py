"""Platform adapter registry, run selection, and strict outcome validation.

Platform-neutral safety kernel: the registry maps a platform key to a
concrete adapter; the generic coordinator dispatches only through this
boundary and never branches on a platform name.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping

from platform_contracts import PlatformSpec
from workflow_store import RunContext
from workflow_types import ProgressEvent, StepName, StepOutcome


class AdapterContractError(RuntimeError):
    """An adapter violated the hardened workflow contract."""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(message or code)


class RunSelection(str, Enum):
    AUTO = "auto"
    RESUME = "resume"
    NEW = "new"


@dataclass(frozen=True)
class WorkflowOptions:
    headless: bool | None = None
    start_minimized: bool = True
    mode: str = "full_auto"
    run_selection: RunSelection = RunSelection.AUTO


@dataclass(frozen=True)
class ResumeDescriptor:
    available: bool
    platform_key: str
    session_id: str | None = None
    run_id: str | None = None
    generation_id: str | None = None
    next_step: StepName | None = None
    processed_count: int = 0
    remaining_count: int = 0
    candidate_count: int = 0
    reason_code: str | None = None
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "platform_key": self.platform_key,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "generation_id": self.generation_id,
            "next_step": self.next_step.value if self.next_step else None,
            "processed_count": self.processed_count,
            "remaining_count": self.remaining_count,
            "candidate_count": self.candidate_count,
            "reason_code": self.reason_code,
            "message": self.message,
        }


@dataclass(frozen=True)
class PreparedWorkflowRun:
    context: RunContext
    resumed: bool
    next_step: StepName


class PlatformWorkflowAdapter(ABC):
    """Service-facing boundary for one platform's workflow."""

    spec: PlatformSpec

    @property
    @abstractmethod
    def session_root(self) -> Path: ...

    @abstractmethod
    def bind_session(self, path) -> None: ...

    @abstractmethod
    def create_session(self) -> Path: ...

    @abstractmethod
    def use_latest_session(self) -> Path: ...

    @abstractmethod
    def prepare_run(
        self, selection: RunSelection, mode: str
    ) -> PreparedWorkflowRun: ...

    @abstractmethod
    def prepare_step(
        self, step: StepName, mode: str = "manual"
    ) -> PreparedWorkflowRun:
        """Bind one manual step to a valid run without implicit fallback.

        Step 1 creates a new run. Later steps reuse the active run and let
        their typed prerequisite checks fail closed when upstream evidence is
        absent or stale.
        """
        ...

    @abstractmethod
    def inspect_resume(self) -> ResumeDescriptor: ...

    @abstractmethod
    def run_vpn_snapshot(
        self,
        prepared: PreparedWorkflowRun,
        options: WorkflowOptions,
        progress: Callable[[ProgressEvent], None],
    ) -> StepOutcome: ...

    @abstractmethod
    def run_local_snapshot(
        self,
        prepared: PreparedWorkflowRun,
        options: WorkflowOptions,
        progress: Callable[[ProgressEvent], None],
    ) -> StepOutcome: ...

    @abstractmethod
    def run_compare(
        self,
        prepared: PreparedWorkflowRun,
        options: WorkflowOptions,
        progress: Callable[[ProgressEvent], None],
    ) -> StepOutcome: ...

    @abstractmethod
    def run_verify(
        self,
        prepared: PreparedWorkflowRun,
        options: WorkflowOptions,
        progress: Callable[[ProgressEvent], None],
    ) -> StepOutcome: ...

    @abstractmethod
    def request_stop(self) -> None: ...

    @abstractmethod
    def clear_stop(self) -> None: ...

    @abstractmethod
    def is_stop_requested(self) -> bool: ...

    @abstractmethod
    def blocked_projection_path(self) -> Path: ...

    @abstractmethod
    def candidate_projection_path(self) -> Path: ...

    @abstractmethod
    def get_master_models(self) -> list[dict]: ...

    @abstractmethod
    def compile_master(self): ...

    @abstractmethod
    def record_restoration(
        self,
        prepared: PreparedWorkflowRun,
        outcome: StepOutcome,
        evidence: Mapping[str, object],
    ) -> None: ...


def validate_step_outcome(
    outcome: object,
    *,
    prepared: PreparedWorkflowRun,
    spec: PlatformSpec,
    expected_step: StepName,
) -> StepOutcome:
    """Accept only a StepOutcome bound to this exact run identity."""
    if not isinstance(outcome, StepOutcome):
        raise AdapterContractError("invalid_step_outcome")
    if outcome.platform != spec.key:
        raise AdapterContractError("step_platform_mismatch")
    if outcome.run_id != prepared.context.run_id:
        raise AdapterContractError("step_run_mismatch")
    if outcome.generation_id != prepared.context.generation_id:
        raise AdapterContractError("step_generation_mismatch")
    if outcome.session_id != prepared.context.session_id:
        raise AdapterContractError("step_session_mismatch")
    if outcome.step is not expected_step:
        raise AdapterContractError("step_name_mismatch")
    return outcome


class PlatformAdapterRegistry:
    """The only mapping from a public platform key to a concrete adapter."""

    def __init__(self):
        self._adapters: dict[str, PlatformWorkflowAdapter] = {}
        self._display_names: dict[str, str] = {}

    def register(
        self, adapter: PlatformWorkflowAdapter, *, display_name: str | None = None
    ) -> None:
        key = adapter.spec.key
        if key in self._adapters:
            raise AdapterContractError(
                "invalid_request", f"platform {key!r} already registered"
            )
        self._adapters[key] = adapter
        display = display_name or adapter.spec.display_name
        self._display_names[display] = key

    def resolve(self, key_or_display: str) -> PlatformWorkflowAdapter:
        key = self._display_names.get(key_or_display, key_or_display)
        adapter = self._adapters.get(key)
        if adapter is None:
            raise AdapterContractError(
                "unsupported_platform", f"unknown platform {key_or_display!r}"
            )
        return adapter

    def keys(self) -> tuple[str, ...]:
        return tuple(self._adapters)

    def display_names(self) -> tuple[str, ...]:
        return tuple(self._display_names)
