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


class DecisionOutcome(str, Enum):
    """一次评估/命令的对外结果（稳定且可区分）。"""

    OK = "ok"  # 正常完成（含 no-op 保持）
    INSUFFICIENT_CAPACITY = "insufficient_capacity"  # 全部可削减容量仍压不到目标区间
    INVALID_INPUT = "invalid_input"  # 输入快照非法，算不出有效净负荷
    INVALID_LOAD_CONFIG = "invalid_load_config"  # 可控负载缺少优先级/容量等必需信息


class ControllableLoad(BaseModel):
    """可控负载配置：priority 数值越大越重要（越晚削减、越早恢复）。"""

    device_id: str
    priority: int
    sheddable_kw: float


class ShedItem(BaseModel):
    """削减计划中对单个负载的一项动作/状态。"""

    device_id: str
    priority: int
    shed_kw: float


class ShedPlan(BaseModel):
    """一份明确的削减计划：目标档位、削减/归还明细、削减后净负荷。"""

    target_level: int = 0
    net_before_kw: float = 0.0  # 快照毛净负荷（未削减口径）
    net_after_kw: float = 0.0  # 应用目标档位后的净负荷
    shed: list[ShedItem] = Field(default_factory=list)  # 目标档位下全部被削减负载（绝对量）
    applied: list[ShedItem] = Field(default_factory=list)  # 本次迁移新削减的
    restored: list[ShedItem] = Field(default_factory=list)  # 本次迁移归还的（按优先级降序）


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
    # 分级削减扩展（默认值保证旧版 JSONL 记录可直接重放）
    outcome: DecisionOutcome = DecisionOutcome.OK
    level_before: int = 0
    level_after: int = 0
    plan: ShedPlan | None = None


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
