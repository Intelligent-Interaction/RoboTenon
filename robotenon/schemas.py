from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

try:  # Pydantic 2 (preferred)
    from pydantic import BaseModel, ConfigDict, Field, field_validator
    _PYDANTIC_V2 = True
except ImportError:  # Pydantic 1, used by common Torch environments
    from pydantic import BaseModel, Field, validator

    _PYDANTIC_V2 = False
    ConfigDict = dict  # type: ignore[misc,assignment]

    def field_validator(*fields: str, **kwargs: Any):  # type: ignore[no-untyped-def]
        mode = kwargs.pop("mode", None)
        # Pydantic 1 calls this option ``pre``; mirror Pydantic 2's
        # ``mode='before'`` so compatibility validators behave identically.
        if mode == "before":
            kwargs["pre"] = True

        def decorate(function):  # type: ignore[no-untyped-def]
            # The source uses the Pydantic 2 ``@classmethod`` ordering.
            raw = function.__func__ if isinstance(function, classmethod) else function
            return validator(*fields, allow_reuse=True, **kwargs)(raw)

        return decorate


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


if _PYDANTIC_V2:

    class RoboModel(BaseModel):
        model_config = ConfigDict(extra="allow", validate_assignment=True)

    class ConfigRoboModel(RoboModel):
        """Strict base for user-authored framework configuration models."""

        model_config = ConfigDict(
            extra="forbid", validate_assignment=True, populate_by_name=True
        )

else:

    class RoboModel(BaseModel):
        class Config:
            extra = "allow"
            validate_assignment = True
            # Several infrastructure configs use a Python-safe field name
            # (for example ``async_enabled``) with a JSON alias (``async``).
            # Accept both spellings on the Pydantic 1 fallback used by
            # common Torch environments.
            allow_population_by_field_name = True

        def model_dump(
            self, *, mode: str | None = None, **kwargs: Any
        ) -> dict[str, Any]:
            if mode == "json":
                return json.loads(self.json(**kwargs))
            return self.dict(**kwargs)

        def model_dump_json(self, **kwargs: Any) -> str:
            # Pydantic 1 accepts the same useful JSON kwargs (indent, exclude, ...).
            return self.json(**kwargs)

        def model_copy(self, *, update=None, deep=False):
            return self.copy(update=update or {}, deep=deep)

        @classmethod
        def model_validate(cls, value: Any):
            return cls.parse_obj(value)

        @classmethod
        def model_validate_json(cls, value: str | bytes):
            return cls.parse_raw(value)

    class ConfigRoboModel(RoboModel):
        """Strict base for user-authored framework configuration models."""

        class Config(RoboModel.Config):
            extra = "forbid"


class ActionType(str, Enum):
    JOINT_POSITION = "joint_position"
    JOINT_VELOCITY = "joint_velocity"
    CARTESIAN_POSE = "cartesian_pose"
    GRIPPER = "gripper"
    NAVIGATION = "navigation"
    WAIT = "wait"
    CUSTOM = "custom"


class Action(RoboModel):
    schema_version: str = "1.0"
    action_id: UUID = Field(default_factory=uuid4)
    experiment_id: str | None = None
    run_id: UUID | None = None
    episode_id: UUID | None = None
    session_id: UUID
    step_id: UUID = Field(default_factory=uuid4)
    issued_at: datetime = Field(default_factory=utc_now)
    deadline_ns: int | None = Field(default=None, ge=0)
    correlation_id: str | None = None
    action_type: ActionType = ActionType.CUSTOM
    payload: dict[str, Any] = Field(default_factory=dict)
    timeout_ms: int = Field(default=1000, ge=1)
    idempotency_key: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ActionChunk(RoboModel):
    """A short sequence of Actions sent from Workflow to the resident Body.

    The chunk is deliberately separate from ``EnvironmentFeedback``: the
    body/cerebellum owns execution timing and interruption, while the
    environment service remains the source of truth for observations.
    """

    chunk_id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    actions: list[Action] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    max_duration_ms: int = Field(default=1000, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ActionExecution(RoboModel):
    """Durable acknowledgement for one action in an :class:`ActionChunk`.

    Body implementations use this record to make retries and partial chunk
    execution explicit.  ``action_id`` is the idempotency key fallback when a
    caller does not provide ``Action.idempotency_key``.
    """

    action_id: UUID
    idempotency_key: str
    status: Literal["accepted", "running", "succeeded", "failed", "interrupted", "rejected", "deadline_exceeded"]
    started_at: datetime | None = None
    ended_at: datetime | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ActionStatus(str, Enum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class ObservationChannel(RoboModel):
    name: str
    modality: str = Field(min_length=1)

    @field_validator("modality", mode="before")
    @classmethod
    def _modality_name(cls, value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("modality must be a non-empty string")
        return value
    uri: str | None = None
    encoding: str | None = None
    shape: list[int] | None = None
    timestamp: datetime = Field(default_factory=utc_now)
    frame_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ObservationRef(RoboModel):
    """Small, serialisable reference to one persisted observation payload.

    ``ctx`` and service messages carry this object instead of an ndarray,
    image buffer or point-cloud object.  ``uri`` is deliberately logical
    (normally ``cache://...``); the observation infrastructure resolves it to
    a local file, shared-memory segment or object-store object at the edge
    where model/tool input is needed.
    """

    uri: str
    modality: str
    media_type: str | None = None
    encoding: str | None = None
    shape: list[int] | None = None
    dtype: str | None = None
    timestamp_ns: int = Field(default=0, ge=0)
    feedback_id: str = ""
    sequence: int = Field(default=0, ge=0)
    sha256: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    # Additional producer metadata is intentionally small and JSON-safe.
    metadata: dict[str, Any] = Field(default_factory=dict)


class ObservationFrame(RoboModel):
    """A camera/sensor frame with explicit policy, provenance and quality."""

    frame_id: str
    role: Literal["policy_input", "record_only", "debug_overlay"]
    modality: str = Field(min_length=1)
    producer: str = Field(min_length=1)

    @field_validator("modality", "producer", mode="before")
    @classmethod
    def _extension_name(cls, value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("modality and producer must be non-empty strings")
        return value
    capture_policy: Literal["every_step", "on_demand", "disabled"] = "every_step"
    camera_id: str | None = None
    source_time_ns: int | None = None
    received_time_ns: int | None = None
    capture_group_id: str | None = None
    shape: list[int] | None = None
    encoding: str | None = None
    color_space: str | None = None
    depth_unit: str | None = None
    data: Any = None
    artifact_ref: dict[str, Any] | None = None
    valid: bool = True
    age_ms: float | None = Field(default=None, ge=0.0)
    dropped_before: int = Field(default=0, ge=0)
    calibration_ref: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ObservationProfile(RoboModel):
    name: str
    required_modalities: list[str] = Field(default_factory=list)
    policy_cameras: list[str] = Field(default_factory=list)
    sample_hz: float = Field(default=10.0, gt=0.0)
    stale_observation_policy: Literal["pause", "drop", "reuse_last"] = "pause"
    record_enabled: bool = True
    debug_enabled: bool = False


class Observation(RoboModel):
    observation_id: UUID = Field(default_factory=uuid4)
    timestamp: datetime = Field(default_factory=utc_now)
    channels: list[ObservationChannel] = Field(default_factory=list)
    frames: list[ObservationFrame] = Field(default_factory=list)
    state: dict[str, Any] = Field(default_factory=dict)


class EnvironmentHealth(RoboModel):
    ready: bool = True
    degraded: bool = False
    message: str | None = None
    checked_at: datetime = Field(default_factory=utc_now)


class SafetyLevel(str, Enum):
    """Global safety severity, ordered from normal (P0) to emergency (P4)."""

    P0 = "P0"  # normal operation
    P1 = "P1"  # advisory/monitor
    P2 = "P2"  # constrained or degraded operation
    P3 = "P3"  # protective pause/stop before another action
    P4 = "P4"  # emergency stop; hardware supervisor must take over

    @property
    def rank(self) -> int:
        return int(self.value[1])


class SafetySignal(RoboModel):
    """A normalized safety observation emitted by an environment/evaluator."""

    code: str
    level: SafetyLevel
    source: str = "evaluator"
    message: str | None = None
    timestamp: datetime = Field(default_factory=utc_now)
    latched: bool = False
    # A transient signal may expire when its producer stops heartbeating.  A
    # latched P3/P4 signal remains active until an explicit recovery.
    expires_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SafetyStateSnapshot(RoboModel):
    """Read-only process-wide safety state exposed to workflow and Portal."""

    level: SafetyLevel = SafetyLevel.P0
    mode: Literal["normal", "monitor", "limited", "pause", "emergency_stop"] = "normal"
    signals: list[SafetySignal] = Field(default_factory=list)
    revision: int = Field(default=0, ge=0)
    updated_at: datetime = Field(default_factory=utc_now)
    incident_id: UUID | None = None
    incident_started_at: datetime | None = None


class MonitorReport(RoboModel):
    """Compliance result emitted by the independent Monitor service."""

    report_id: UUID = Field(default_factory=uuid4)
    session_id: UUID | None = None
    checked_at: datetime = Field(default_factory=utc_now)
    compliant: bool = True
    safety_level: SafetyLevel = SafetyLevel.P0
    signals: list[SafetySignal] = Field(default_factory=list)
    reason: str | None = None
    source: str = "monitor"
    metadata: dict[str, Any] = Field(default_factory=dict)


class EnvironmentSignal(RoboModel):
    name: str
    value: Any = None
    source: str = "environment"
    timestamp: datetime = Field(default_factory=utc_now)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    safety_level: SafetyLevel | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class EnvironmentCommand(RoboModel):
    command_id: UUID = Field(default_factory=uuid4)
    type: Literal["reset", "pause", "resume", "stop", "capture_frame", "reconcile_action", "custom"]
    profile: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    timeout_ms: int = Field(default=1000, ge=1)


class CameraSpec(RoboModel):
    role: str = "camera"
    flip_vertical: bool = False


class ActionSpaceSpec(RoboModel):
    format: str
    dimensions: int = Field(ge=1)
    robot_type: str | None = None


class EnvironmentIO(RoboModel):
    """Environment-owned sensor semantics and native action contract."""
    cameras: dict[str, CameraSpec] = Field(default_factory=dict)
    state_keys: list[str] = Field(default_factory=list)
    action: ActionSpaceSpec | None = None


class EnvironmentFeedback(RoboModel):
    """Canonical observation returned by every environment step."""

    io: EnvironmentIO = Field(default_factory=EnvironmentIO)
    schema_version: str = "1.0"
    feedback_id: UUID = Field(default_factory=uuid4)
    experiment_id: str | None = None
    run_id: UUID | None = None
    episode_id: UUID | None = None
    session_id: UUID
    sequence_no: int = Field(default=0, ge=0)
    # Observation-cache vocabulary.  The canonical fields above remain the
    # compatibility contract used by existing Workflow code; these aliases
    # let cache-aware callers exchange a compact frame map without carrying
    # raw media in the feedback/context object.
    epoch: int = Field(default=0, ge=0)
    sequence: int = Field(default=0, ge=0)
    timestamp_ns: int | None = Field(default=None, ge=0)
    timestamp: datetime = Field(default_factory=utc_now)
    source_time_ns: int | None = Field(default=None, ge=0)
    received_time_ns: int | None = Field(default=None, ge=0)
    correlation_id: str | None = None
    observation: Observation = Field(default_factory=Observation)
    frames: dict[str, ObservationRef] = Field(default_factory=dict)
    action_id: UUID | None = None
    action_status: ActionStatus = ActionStatus.UNKNOWN
    signals: list[EnvironmentSignal] = Field(default_factory=list)
    safety_level: SafetyLevel = SafetyLevel.P0
    state: dict[str, Any] = Field(default_factory=dict)
    reward: float | None = None
    info: dict[str, Any] = Field(default_factory=dict)
    health: EnvironmentHealth = Field(default_factory=EnvironmentHealth)
    terminal: bool = False
    success: bool | None = None
    error: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def __init__(self, **data: Any):
        # ``sequence_no`` is the original RoboTenon spelling while the
        # observation-cache contract uses ``sequence``.  Keep both populated
        # when callers provide either one; this works on both Pydantic 1 and
        # Pydantic 2 without relying on version-specific model validators.
        original_session = data.get("session_id")
        if original_session is not None:
            try:
                UUID(str(original_session))
            except (ValueError, TypeError, AttributeError):
                data["session_id"] = uuid5(
                    NAMESPACE_URL, f"robotenon:session:{original_session}"
                )
                metadata = dict(data.get("metadata") or {})
                metadata.setdefault("_cache_session_id", str(original_session))
                data["metadata"] = metadata
        original_feedback = data.get("feedback_id")
        if original_feedback is not None:
            try:
                UUID(str(original_feedback))
            except (ValueError, TypeError, AttributeError):
                data["feedback_id"] = uuid5(
                    NAMESPACE_URL, f"robotenon:feedback:{original_feedback}"
                )
                metadata = dict(data.get("metadata") or {})
                metadata.setdefault("_cache_feedback_id", str(original_feedback))
                data["metadata"] = metadata
        if "sequence_no" not in data and "sequence" in data:
            data["sequence_no"] = data["sequence"]
        elif "sequence" not in data and "sequence_no" in data:
            data["sequence"] = data["sequence_no"]
        super().__init__(**data)


class BodyExecutionStatus(str, Enum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    REJECTED = "rejected"


class BodyExecutionResult(RoboModel):
    """Result returned by the resident body after execution/interruption."""

    chunk_id: UUID | None = None
    session_id: UUID | None = None
    status: BodyExecutionStatus = BodyExecutionStatus.SUCCEEDED
    executed_actions: int = Field(default=0, ge=0)
    interrupted: bool = False
    safety_level: SafetyLevel = SafetyLevel.P0
    error: str | None = None
    feedback: EnvironmentFeedback | None = None
    action_results: list[ActionExecution] = Field(default_factory=list)
    deadline_exceeded: bool = False
    acknowledged_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EnvironmentDescriptor(RoboModel):
    env_id: str
    env_version: str = "0.1.0"
    display_name: str | None = None
    capabilities: set[str] = Field(default_factory=set)
    action_types: set[ActionType] = Field(default_factory=set)
    observation_modalities: set[str] = Field(default_factory=set)
    vendor: str | None = None
    backend_kind: Literal["local", "remote", "replay", "unknown"] = "unknown"
    cameras: list[str] = Field(default_factory=list)
    supports_offscreen_render: bool = False
    supports_video_recording: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionSpec(RoboModel):
    task_id: str
    environment_id: str
    experiment_id: str | None = None
    run_id: UUID | None = None
    episode_id: UUID | None = None
    observation_profile: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    seed: int | None = None
    tags: dict[str, str] = Field(default_factory=dict)


class Task(RoboModel):
    task_id: str
    instruction: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    success_conditions: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExperimentSpec(RoboModel):
    """Input declaration that is frozen before a Run starts."""

    experiment_id: str
    task: Task
    environment_id: str
    evaluator_profile: str = "terminal@1"
    system_release_ref: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    episodes: int = Field(default=1, ge=1)
    max_duration_s: float = Field(default=180.0, gt=0.0)
    seed: int | None = None
    tags: dict[str, str] = Field(default_factory=dict)


class TaskStatus(str, Enum):
    """Lifecycle of one executable item in :class:`TaskQueue`."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskItem(RoboModel):
    """One persistent executable item claimed by a Workflow."""

    task_item_id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    task_id: str
    goal: str
    ordinal: int = Field(default=0, ge=0)
    status: TaskStatus = TaskStatus.PENDING
    attempts: int = Field(default=0, ge=0)
    timeout_s: float = Field(default=30.0, gt=0.0)
    max_retries: int = Field(default=2, ge=0)
    on_failure: Literal["retry", "replan", "abort"] = "retry"
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    parent_id: UUID | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

class EvaluationInput(RoboModel):
    experiment_id: str | None = None
    run_id: UUID | None = None
    episode_id: UUID | None = None
    session_id: UUID
    feedback: EnvironmentFeedback
    context_revision: int = Field(default=0, ge=0)
    action: Action | None = None
    task: TaskItem | None = None


class QueueOperation(str, Enum):
    """Queue command requested by an evaluator decision."""

    NEXT = "next"       # complete current item and claim the next one
    RETRY = "retry"     # return current item to pending when retries remain
    CLEAR = "clear"     # cancel all pending/running items
    BRANCH = "branch"   # add branch_tasks and continue with the next item
    HOLD = "hold"       # keep current item pending/running; do not advance


class EvaluationDecisionType(str, Enum):
    CONTINUE = "continue"
    COMPLETE = "complete"
    RETRY = "retry"
    ABORT = "abort"
    PAUSE = "pause"
    FINISH = "complete"  # design vocabulary alias
    INTERRUPT = "pause"
    REPLAN = "retry"
    EMERGENCY_STOP = "abort"


class EvaluationDecision(RoboModel):
    decision_id: UUID = Field(default_factory=uuid4)
    experiment_id: str | None = None
    run_id: UUID | None = None
    episode_id: UUID | None = None
    session_id: UUID
    decision: EvaluationDecisionType
    reason_code: str
    reason: str | None = None
    based_on_feedback_id: UUID
    based_on_sequence_no: int = Field(ge=0)
    context_revision: int = Field(default=0, ge=0)
    emitted_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)
    task_success: bool | None = None
    task_item_success: bool | None = None
    safety_level: SafetyLevel = SafetyLevel.P0
    safety_signals: list[SafetySignal] = Field(default_factory=list)
    queue_operation: QueueOperation = QueueOperation.HOLD
    branch_tasks: list[TaskItem] = Field(default_factory=list)

class MetricSample(RoboModel):
    name: str
    value: float
    experiment_id: str | None = None
    attempt_id: str | None = None
    task_id: str | None = None
    adapter_id: str | None = None
    worker_id: str | None = None
    gpu_id: str | None = None
    episode_id: UUID | str | None = None
    timestamp: datetime = Field(default_factory=utc_now)
    session_id: UUID | None = None
    run_id: UUID | str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    tags: dict[str, str] | None = None
    # Optional finite, strictly increasing upper bounds for histogram
    # buckets.  When omitted, the resident MetricsDaemon applies its
    # reusable default bucket layout (or a registered metric definition).
    histogram_buckets: tuple[float, ...] | None = None
    kind: Literal["counter", "gauge", "histogram", "event"] = "gauge"
    scope: Literal["step", "task_item", "episode", "run"] = "step"
    unit: str | None = None
    provisional: bool = True
    final: bool = False

    @field_validator("histogram_buckets")
    @classmethod
    def _valid_histogram_buckets(cls, value: tuple[float, ...] | None) -> tuple[float, ...] | None:
        if value is None:
            return None
        normalized = tuple(float(item) for item in value)
        if not normalized or any(not math.isfinite(item) for item in normalized):
            raise ValueError("histogram_buckets must contain finite values")
        if tuple(sorted(set(normalized))) != normalized:
            raise ValueError("histogram_buckets must be strictly increasing")
        return normalized


class TraceSpan(RoboModel):
    trace_id: UUID = Field(default_factory=uuid4)
    span_id: UUID = Field(default_factory=uuid4)
    experiment_id: str | None = None
    attempt_id: str | None = None
    task_id: str | None = None
    adapter_id: str | None = None
    worker_id: str | None = None
    gpu_id: str | None = None
    run_id: UUID | str | None = None
    episode_id: UUID | str | None = None
    parent_span_id: UUID | None = None
    name: str
    start_time: datetime = Field(default_factory=utc_now)
    end_time: datetime | None = None
    status: Literal["ok", "error", "unset"] = "unset"
    attributes: dict[str, Any] = Field(default_factory=dict)

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        events = self.attributes.setdefault("events", [])
        if isinstance(events, list):
            events.append({"name": name, "attributes": attributes or {}, "timestamp": utc_now().isoformat()})


class EventEnvelope(RoboModel):
    event_id: UUID = Field(default_factory=uuid4)
    event_type: str
    emitted_at: datetime = Field(default_factory=utc_now)
    experiment_id: str | None = None
    run_id: UUID | None = None
    episode_id: UUID | None = None
    session_id: UUID | None = None
    sequence_no: int | None = Field(default=None, ge=0)
    correlation_id: str | None = None
    trace_id: UUID | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ArtifactRef(RoboModel):
    uri: str
    sha256: str | None = None
    media_type: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)


class MemoryRecord(RoboModel):
    memory_id: UUID = Field(default_factory=uuid4)
    task_id: str
    summary: str
    outcome: Literal["success", "failure", "partial", "unknown"] = "unknown"
    environment_id: str | None = None
    source_run_id: UUID | None = None
    tags: dict[str, str] = Field(default_factory=dict)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class SkillHead(RoboModel):
    skill_id: str
    name: str
    description: str
    version: str
    compatible_capabilities: set[str] = Field(default_factory=set)
    risk_level: Literal["low", "medium", "high"] = "medium"


class SkillDefinition(SkillHead):
    input_schema: dict[str, Any] = Field(default_factory=dict)
    completion_conditions: list[str] = Field(default_factory=list)
    failure_codes: list[str] = Field(default_factory=list)
    entrypoint: str
    content_hash: str | None = None


class DatasetSnapshot(RoboModel):
    dataset_id: str
    version: str
    created_at: datetime = Field(default_factory=utc_now)
    source_run_ids: list[UUID] = Field(default_factory=list)
    split: dict[str, list[UUID]] = Field(default_factory=dict)
    schema_version: str = "1.0"
    transform_version: str = "identity@1"
    query: dict[str, Any] = Field(default_factory=dict)
    content_hash: str | None = None


class SystemArtifact(RoboModel):
    artifact_id: str
    name: str
    version: str
    model_uri: str
    runtime_uri: str | None = None
    action_schema_version: str = "1.0"
    feedback_schema_version: str = "1.0"
    config: dict[str, Any] = Field(default_factory=dict)
    content_hash: str


class SystemRelease(RoboModel):
    release_id: str
    artifact: SystemArtifact
    status: Literal["draft", "validating", "candidate", "released", "deprecated", "revoked"] = "draft"
    validation_refs: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class RunManifest(RoboModel):
    run_id: UUID
    session_id: UUID
    task_id: str
    environment_id: str
    started_at: datetime
    ended_at: datetime | None = None
    status: Literal["running", "succeeded", "failed", "cancelled"] = "running"
    success: bool | None = None
    config_hash: str | None = None
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    counters: dict[str, float] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("ended_at")
    @classmethod
    def _timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
