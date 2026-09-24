"""领域模型与 API 出入参类型（骨架：仅类型声明）。"""

import enum
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import BaseModel, Field


class DeviceType(str, enum.Enum):
    BATTERY = "battery"
    SOLAR = "solar"
    GRID = "grid"
    LOAD = "load"


class EventKind(str, enum.Enum):
    TELEMETRY = "telemetry"
    COMMAND = "command"
    DECISION = "decision"


class PointStatus(str, enum.Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"


class RejectReason(str, enum.Enum):
    UNKNOWN_DEVICE = "unknown_device"
    UNKNOWN_METRIC = "unknown_metric"
    ILLEGAL_UNIT = "illegal_unit"
    MISSING_FIELD = "missing_field"
    MALFORMED_VALUE = "malformed_value"
    VALUE_OUT_OF_RANGE = "value_out_of_range"
    BAD_TIMESTAMP = "bad_timestamp"
    STALE_TIMESTAMP = "stale_timestamp"
    FUTURE_TIMESTAMP = "future_timestamp"
    OUT_OF_ORDER = "out_of_order"
    BATCH_EMPTY = "batch_empty"


class CommandType(str, enum.Enum):
    CURTAIL = "curtail"
    RELEASE = "release"
    SET_LIMIT = "set_limit"


class CommandStatus(str, enum.Enum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"
    CONFLICT = "conflict"


# ---------- API 入参 ----------


class TelemetryPointIn(BaseModel):
    device_id: str
    metric: str
    value: float
    unit: str
    ts: str
    client_event_id: str | None = None


class TelemetryBatchIn(BaseModel):
    points: list[TelemetryPointIn] = Field(default_factory=list)
    batch_id: str | None = None


class CommandIn(BaseModel):
    command_id: str
    command_type: CommandType
    target_load_id: str | None = None
    limit_w: float | None = None
    reason: str | None = None


# ---------- API 出参 ----------


class PointResultOut(BaseModel):
    index: int
    status: PointStatus
    reason: str | None = None
    event_id: str | None = None
    detail: str | None = None


class BatchResultOut(BaseModel):
    batch_id: str | None = None
    accepted: int = 0
    duplicate: int = 0
    rejected: int = 0
    results: list[PointResultOut] = Field(default_factory=list)


class CommandResultOut(BaseModel):
    command_id: str
    status: CommandStatus
    decision_id: str | None = None
    reason: str | None = None


# ---------- 内部领域对象 ----------


@dataclass(frozen=True)
class Event:
    seq: int
    event_id: str
    kind: EventKind
    received_at: datetime
    occurred_at: datetime | None
    device_id: str | None
    metric: str | None
    value: float | None
    unit: str | None
    dedupe_key: str | None
    payload: dict


@dataclass(frozen=True)
class MetricReading:
    device_id: str
    metric: str
    value: float
    unit: str
    ts: datetime
    event_id: str


@dataclass
class DeviceSnapshot:
    device_id: str
    device_type: DeviceType
    readings: dict[str, MetricReading] = field(default_factory=dict)


@dataclass
class SystemSnapshot:
    generated_at: datetime
    devices: dict[str, DeviceSnapshot]
    battery_soc: float | None
    solar_generation_w: float | None
    load_power_w: float | None
    net_load_w: float | None


@dataclass(frozen=True)
class Decision:
    decision_id: str
    decided_at: datetime
    curtailed: bool
    limit_w: float | None
    trigger: str
    reasons: list[str]
    snapshot: dict
    thresholds: dict


@dataclass(frozen=True)
class CommandResult:
    command_id: str
    status: CommandStatus
    decision_id: str | None
    reason: str | None
    applied_at: datetime | None
