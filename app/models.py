"""数据模型与枚举定义。"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class IngestStatus(str, Enum):
    """单条遥测数据的处理结果。"""

    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"


class RejectReason(str, Enum):
    """可区分的拒绝原因。"""

    MISSING_FIELD = "missing_field"
    UNKNOWN_DEVICE = "unknown_device"
    UNKNOWN_METRIC = "unknown_metric"
    INVALID_UNIT = "invalid_unit"
    INVALID_VALUE = "invalid_value"
    STALE_TIMESTAMP = "stale_timestamp"
    FUTURE_TIMESTAMP = "future_timestamp"
    OUT_OF_ORDER = "out_of_order"


class SystemMode(str, Enum):
    """系统运行模式（带滞回的状态机）。"""

    NORMAL = "normal"
    SHEDDING = "shedding"


class LoadAction(str, Enum):
    """调度动作。"""

    NONE = "none"
    SHED = "shed"  # 触发限载
    RESTORE = "restore"  # 解除限载


class IngestResult(BaseModel):
    """单条遥测的稳定、可区分处理结果。"""

    status: IngestStatus
    event_id: str | None = None
    reason: RejectReason | None = None
    detail: str = ""


class BatchIngestResponse(BaseModel):
    """批量提交的汇总结果；重复提交同一批数据结果完全一致。"""

    results: list[IngestResult]
    accepted: int
    duplicates: int
    rejected: int


class DeviceSnapshot(BaseModel):
    """单设备最新遥测快照。"""

    device_id: str
    metrics: dict[str, float] = Field(default_factory=dict)
    last_seen: datetime | None = None


class SystemSnapshot(BaseModel):
    """系统级实时状态快照（调度判定的输入）。"""

    load_kw: float = 0.0
    generation_kw: float = 0.0
    storage_soc: float = 1.0
    net_load_kw: float = 0.0
    devices: dict[str, DeviceSnapshot] = Field(default_factory=dict)


class DecisionRecord(BaseModel):
    """一次调度决策，可追溯到输入快照与判定依据。"""

    decision_id: str
    decided_at: datetime
    mode_before: SystemMode
    mode_after: SystemMode
    action: LoadAction
    reasons: list[str] = Field(default_factory=list)
    snapshot: SystemSnapshot
    thresholds: dict[str, float] = Field(default_factory=dict)


# 已注册的指标及其合法单位（未知指标不破坏既有查询，仅被拒绝并记录原因）
KNOWN_METRICS: dict[str, set[str]] = {
    "power": {"kW", "W"},
    "energy": {"kWh", "Wh"},
    "voltage": {"V"},
    "current": {"A"},
    "soc": {"percent", "ratio"},
    "temperature": {"C"},
}

# 单位归一化系数（归一到 kW / kWh / ratio 等基准单位）
UNIT_SCALE: dict[str, tuple[str, float]] = {
    "W": ("kW", 0.001),
    "kW": ("kW", 1.0),
    "Wh": ("kWh", 0.001),
    "kWh": ("kWh", 1.0),
    "V": ("V", 1.0),
    "A": ("A", 1.0),
    "percent": ("ratio", 0.01),
    "ratio": ("ratio", 1.0),
    "C": ("C", 1.0),
}

REQUIRED_FIELDS = ("event_id", "device_id", "metric", "value", "unit", "timestamp")

# 遥测载荷中允许出现但会被忽略的扩展字段之外的键不参与校验，
# 未知的新字段不破坏既有处理（前向兼容）。
KNOWN_OPTIONAL_FIELDS: set[str] = set()


def make_event(payload: dict[str, Any]) -> dict[str, Any]:
    """从原始载荷构造内部事件（仅骨架辅助，具体校验在 telemetry 中）。"""
    return dict(payload)
