"""Serializable contracts shared by ModelScraper workflows and interfaces."""

from __future__ import annotations

import datetime as _datetime
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping


def utc_now_iso() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat(
        timespec="seconds"
    )


class OutcomeStatus(str, Enum):
    SUCCEEDED = "succeeded"
    CANCELLED = "cancelled"
    FAILED = "failed"
    INCOMPLETE = "incomplete"
    # Legacy-read compatibility only. A hardened adapter never returns a
    # waiting outcome; runtime waiting is a ProgressStatus, not a terminal
    # step status.
    WAITING_FOR_USER = "waiting_for_user"


class ProgressStatus(str, Enum):
    """Status vocabulary for active progress events (schema v2).

    Terminal truth lives exclusively in StepOutcome/RunOutcome. An active
    callback can only ever be running or waiting for the user.
    """

    RUNNING = "running"
    WAITING_FOR_USER = "waiting_for_user"


class StepName(str, Enum):
    VPN_SNAPSHOT = "step1_vpn_snapshot"
    LOCAL_SNAPSHOT = "step2_local_snapshot"
    COMPARE = "step3_compare"
    VERIFY = "step4_verify"
    RESTORE = "restore_machine_policy"


class VerificationVerdict(str, Enum):
    BLOCKED = "blocked"
    ACCESSIBLE = "accessible"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ArtifactRecord:
    path: str
    record_count: int
    byte_size: int
    sha256: str
    generation_id: str
    created_at: str
    artifact_type: str = ""
    logical_name: str = ""
    schema: str = ""
    platform_key: str = ""
    session_id: str = ""
    run_id: str = ""
    producer_step: str = ""
    upstream_sha256: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["upstream_sha256"] = dict(self.upstream_sha256)
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ArtifactRecord":
        if not isinstance(value, dict):
            raise ValueError("artifact record must be an object")
        required = (
            "path",
            "record_count",
            "byte_size",
            "sha256",
            "generation_id",
            "created_at",
        )
        if any(key not in value for key in required):
            raise ValueError("artifact record is missing required fields")
        path = str(value["path"])
        if not path or path.startswith(("/", "\\")) or ":" in path:
            raise ValueError("artifact path must be relative")
        upstream = value.get("upstream_sha256") or {}
        if not isinstance(upstream, dict):
            raise ValueError("upstream_sha256 must be an object")
        return cls(
            path=path,
            record_count=max(0, int(value["record_count"])),
            byte_size=max(0, int(value["byte_size"])),
            sha256=str(value["sha256"]),
            generation_id=str(value["generation_id"]),
            created_at=str(value["created_at"]),
            artifact_type=str(value.get("artifact_type") or ""),
            logical_name=str(value.get("logical_name") or ""),
            schema=str(value.get("schema") or ""),
            platform_key=str(value.get("platform_key") or ""),
            session_id=str(value.get("session_id") or ""),
            run_id=str(value.get("run_id") or ""),
            producer_step=str(value.get("producer_step") or ""),
            upstream_sha256={
                str(key): str(item) for key, item in upstream.items()
            },
        )


@dataclass(frozen=True)
class StepOutcome:
    run_id: str
    generation_id: str
    platform: str
    step: StepName
    status: OutcomeStatus
    started_at: str
    finished_at: str
    input_count: int = 0
    output_count: int = 0
    processed_count: int = 0
    remaining_count: int = 0
    warnings: tuple[str, ...] = ()
    error_code: str | None = None
    error_message: str | None = None
    artifacts: tuple[ArtifactRecord, ...] = ()
    resumable: bool = False
    retryable: bool = False
    waiting_reason: str | None = None
    summary_counts: Mapping[str, int] = field(default_factory=dict)
    session_id: str = ""
    new_generation_required: bool = False

    @property
    def can_continue(self) -> bool:
        return self.status is OutcomeStatus.SUCCEEDED

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["step"] = self.step.value
        value["status"] = self.status.value
        value["warnings"] = list(self.warnings)
        value["artifacts"] = [item.to_dict() for item in self.artifacts]
        value["summary_counts"] = dict(self.summary_counts)
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "StepOutcome":
        if not isinstance(value, dict):
            raise ValueError("step outcome must be an object")
        required = (
            "run_id",
            "generation_id",
            "platform",
            "step",
            "status",
            "started_at",
            "finished_at",
        )
        if any(key not in value for key in required):
            raise ValueError("step outcome is missing required fields")
        return cls(
            run_id=str(value["run_id"]),
            generation_id=str(value["generation_id"]),
            platform=str(value["platform"]),
            step=StepName(value["step"]),
            status=OutcomeStatus(value["status"]),
            started_at=str(value["started_at"]),
            finished_at=str(value["finished_at"]),
            input_count=max(0, int(value.get("input_count", 0))),
            output_count=max(0, int(value.get("output_count", 0))),
            processed_count=max(0, int(value.get("processed_count", 0))),
            remaining_count=max(0, int(value.get("remaining_count", 0))),
            warnings=tuple(str(item) for item in value.get("warnings", ())),
            error_code=(
                str(value["error_code"]) if value.get("error_code") else None
            ),
            error_message=(
                str(value["error_message"])
                if value.get("error_message")
                else None
            ),
            artifacts=tuple(
                ArtifactRecord.from_dict(item)
                for item in value.get("artifacts", ())
            ),
            resumable=bool(value.get("resumable", False)),
            retryable=bool(value.get("retryable", False)),
            waiting_reason=(
                str(value["waiting_reason"])
                if value.get("waiting_reason")
                else None
            ),
            summary_counts={
                str(key): int(item)
                for key, item in (value.get("summary_counts") or {}).items()
            },
            session_id=str(value.get("session_id") or ""),
            new_generation_required=bool(
                value.get("new_generation_required", False)
            ),
        )

    @classmethod
    def _create(
        cls,
        status: OutcomeStatus,
        *,
        run_id: str,
        generation_id: str,
        platform: str,
        step: StepName,
        started_at: str | None = None,
        finished_at: str | None = None,
        **values: Any,
    ) -> "StepOutcome":
        finished_at = finished_at or utc_now_iso()
        return cls(
            run_id=run_id,
            generation_id=generation_id,
            platform=platform,
            step=step,
            status=status,
            started_at=started_at or finished_at,
            finished_at=finished_at,
            **values,
        )

    @classmethod
    def succeeded(cls, **values: Any) -> "StepOutcome":
        return cls._create(OutcomeStatus.SUCCEEDED, **values)

    @classmethod
    def cancelled(cls, **values: Any) -> "StepOutcome":
        return cls._create(OutcomeStatus.CANCELLED, **values)

    @classmethod
    def failed(cls, **values: Any) -> "StepOutcome":
        return cls._create(OutcomeStatus.FAILED, **values)

    @classmethod
    def incomplete(cls, **values: Any) -> "StepOutcome":
        return cls._create(OutcomeStatus.INCOMPLETE, **values)


@dataclass(frozen=True)
class VerificationRecord:
    original_url: str
    verdict: VerificationVerdict
    reason_code: str
    attempt: int
    timestamp: str
    observed_url: str
    diagnostic_summary: str = ""
    run_id: str = ""
    generation_id: str = ""
    epoch: int = 0
    epoch_attempt: int = 0
    retryable: bool = False
    terminal: bool = False
    evidence_code: str = ""

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["verdict"] = self.verdict.value
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "VerificationRecord":
        return cls(
            original_url=str(value["original_url"]),
            verdict=VerificationVerdict(value["verdict"]),
            reason_code=str(value["reason_code"]),
            attempt=max(1, int(value["attempt"])),
            timestamp=str(value["timestamp"]),
            observed_url=str(value.get("observed_url") or ""),
            diagnostic_summary=str(value.get("diagnostic_summary") or "")[:240],
            run_id=str(value.get("run_id") or ""),
            generation_id=str(value.get("generation_id") or ""),
            epoch=max(0, int(value.get("epoch", 0))),
            epoch_attempt=max(0, int(value.get("epoch_attempt", 0))),
            retryable=bool(value.get("retryable", False)),
            terminal=bool(value.get("terminal", False)),
            evidence_code=str(value.get("evidence_code") or ""),
        )


@dataclass(frozen=True)
class ProgressEvent:
    run_id: str
    generation_id: str
    platform: str
    step: StepName
    phase: str
    completed: int
    total: int | None
    message: str
    status: ProgressStatus
    sequence: int = 0
    occurred_at: str = ""
    requires_user: bool = False
    summary_counts: Mapping[str, int] = field(default_factory=dict)
    waiting_reason: str | None = None
    session_id: str = ""
    task_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.status, ProgressStatus):
            raise TypeError("ProgressEvent.status must be ProgressStatus")

    @property
    def percent(self) -> float | None:
        if self.total is None or self.total <= 0:
            return None
        return round(min(100.0, max(0.0, self.completed * 100.0 / self.total)), 2)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["step"] = self.step.value
        value["status"] = self.status.value
        value["percent"] = self.percent
        value["summary_counts"] = dict(self.summary_counts)
        return value

    @staticmethod
    def normalize_legacy_status(value: Any) -> ProgressStatus:
        """Map a v1 progress status string onto the v2 vocabulary.

        Only ``ProgressEvent.from_dict()`` and named v1 compatibility views
        may call this; it must never run while parsing a StepOutcome or
        RunOutcome.
        """
        if isinstance(value, ProgressStatus):
            return value
        text = str(value)
        if text in ("succeeded", "incomplete", "running"):
            return ProgressStatus.RUNNING
        if text == "waiting_for_user":
            return ProgressStatus.WAITING_FOR_USER
        raise ValueError("invalid_legacy_progress_status")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ProgressEvent":
        if not isinstance(value, dict):
            raise ValueError("progress event must be an object")
        required = (
            "run_id",
            "generation_id",
            "platform",
            "step",
            "phase",
            "completed",
            "message",
            "status",
        )
        if any(key not in value for key in required):
            raise ValueError("progress event is missing required fields")
        total = value.get("total")
        return cls(
            run_id=str(value["run_id"]),
            generation_id=str(value["generation_id"]),
            platform=str(value["platform"]),
            step=StepName(value["step"]),
            phase=str(value["phase"]),
            completed=max(0, int(value["completed"])),
            total=None if total is None else max(0, int(total)),
            message=str(value["message"]),
            status=cls.normalize_legacy_status(value["status"]),
            sequence=max(0, int(value.get("sequence", 0))),
            occurred_at=str(value.get("occurred_at") or ""),
            requires_user=bool(value.get("requires_user", False)),
            summary_counts={
                str(key): int(item)
                for key, item in (value.get("summary_counts") or {}).items()
            },
            waiting_reason=(
                str(value["waiting_reason"])
                if value.get("waiting_reason")
                else None
            ),
            session_id=str(value.get("session_id") or ""),
            task_id=str(value.get("task_id") or ""),
        )


@dataclass(frozen=True)
class RunOutcome:
    run_id: str
    generation_id: str
    platform: str
    mode: str
    status: OutcomeStatus
    steps: tuple[StepOutcome, ...]
    restoration: StepOutcome | None
    started_at: str
    finished_at: str
    summary_counts: dict[str, int] = field(default_factory=dict)
    last_resumable_step: StepName | None = None
    error_code: str | None = None
    message: str = ""
    session_id: str = ""

    @classmethod
    def calculate(
        cls,
        *,
        run_id: str,
        generation_id: str,
        platform: str,
        mode: str,
        steps: Iterable[StepOutcome],
        restoration: StepOutcome | None,
        started_at: str,
        finished_at: str | None = None,
        message: str = "",
        session_id: str = "",
        required_steps: tuple[StepName, ...] | None = None,
    ) -> "RunOutcome":
        ordered = tuple(steps)
        waiting = next(
            (
                item
                for item in ordered
                if item.status is OutcomeStatus.WAITING_FOR_USER
            ),
            None,
        )
        if waiting is not None:
            # A waiting StepOutcome is a contract violation: waiting is a
            # runtime ProgressStatus, never a terminal step truth.
            ordered = tuple(
                item
                for item in ordered
                if item.status is not OutcomeStatus.WAITING_FOR_USER
            ) + (
                StepOutcome.failed(
                    run_id=waiting.run_id,
                    generation_id=waiting.generation_id,
                    platform=waiting.platform,
                    step=waiting.step,
                    error_code="invalid_waiting_outcome",
                    error_message=(
                        "A step returned WAITING_FOR_USER as a terminal "
                        "outcome."
                    ),
                    session_id=waiting.session_id,
                ),
            )
        failed = next(
            (item for item in ordered if item.status is OutcomeStatus.FAILED),
            None,
        )
        cancelled = next(
            (
                item
                for item in ordered
                if item.status is OutcomeStatus.CANCELLED
            ),
            None,
        )
        incomplete = next(
            (
                item
                for item in ordered
                if item.status is OutcomeStatus.INCOMPLETE
            ),
            None,
        )
        if restoration and restoration.status is not OutcomeStatus.SUCCEEDED:
            status = OutcomeStatus.FAILED
            error_code = restoration.error_code or "restoration_failed"
        elif failed:
            status = OutcomeStatus.FAILED
            error_code = failed.error_code
        elif cancelled:
            status = OutcomeStatus.CANCELLED
            error_code = cancelled.error_code
        elif incomplete:
            status = OutcomeStatus.INCOMPLETE
            error_code = incomplete.error_code
        elif ordered and all(item.can_continue for item in ordered):
            present = {item.step for item in ordered}
            if required_steps is not None and (
                set(required_steps) - present
                or len([item for item in ordered if item.step in required_steps])
                != len(required_steps)
            ):
                status = OutcomeStatus.INCOMPLETE
                error_code = "missing_step_outcome"
            else:
                status = OutcomeStatus.SUCCEEDED
                error_code = None
        else:
            status = OutcomeStatus.INCOMPLETE
            error_code = "missing_step_outcome"
        resumable = next(
            (
                item.step
                for item in reversed(ordered)
                if item.status
                in (OutcomeStatus.CANCELLED, OutcomeStatus.INCOMPLETE)
            ),
            None,
        )
        return cls(
            run_id=run_id,
            generation_id=generation_id,
            platform=platform,
            mode=mode,
            status=status,
            steps=ordered,
            restoration=restoration,
            started_at=started_at,
            finished_at=finished_at or utc_now_iso(),
            summary_counts={
                "input": sum(item.input_count for item in ordered),
                "output": sum(item.output_count for item in ordered),
                "processed": sum(item.processed_count for item in ordered),
                "remaining": sum(item.remaining_count for item in ordered),
            },
            last_resumable_step=resumable,
            error_code=error_code,
            message=message,
            session_id=session_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "generation_id": self.generation_id,
            "platform": self.platform,
            "mode": self.mode,
            "session_id": self.session_id,
            "status": self.status.value,
            "steps": [item.to_dict() for item in self.steps],
            "restoration": (
                self.restoration.to_dict() if self.restoration else None
            ),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "summary_counts": dict(self.summary_counts),
            "last_resumable_step": (
                self.last_resumable_step.value
                if self.last_resumable_step
                else None
            ),
            "error_code": self.error_code,
            "message": self.message,
        }
