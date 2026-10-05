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


class PlanStatus(str, Enum):
    """分级削减计划的对外结果（稳定且可区分）。"""

    OK = "ok"  # 计划完整可执行（含无需动作的保持）
    INSUFFICIENT_CAPACITY = "insufficient_capacity"  # 全部可削减容量仍压不到目标区间
    MISSING_LOAD_CONFIG = "missing_load_config"  # 有可控负载缺优先级/容量等必需信息
    INVALID_INPUT = "invalid_input"  # 输入状态不合法，算不出有效净负荷


class ControllableLoad(BaseModel):
    """可控负载的分级配置：优先级数值越小越先被削减。"""

    device_id: str
    priority: int
    sheddable_kw: float


class ShedStep(BaseModel):
    """计划中对单个负载的削减步（按削减顺序排列）。"""

    device_id: str
    priority: int
    shed_kw: float


class ShedPlan(BaseModel):
    """一份确定的分级削减计划：目标档位、各负载削减量、削减后净负荷。"""

    level: int = 0  # 目标档位：0=不削减，L=按优先级顺序削减前 L 个负载
    steps: list[ShedStep] = Field(default_factory=list)
    total_shed_kw: float = 0.0
    projected_net_load_kw: float | None = None  # 削减后预计净负荷
    target_net_load_kw: float | None = None  # 目标区间上沿（须严格落入其下）
    status: PlanStatus = PlanStatus.OK
    reasons: list[str] = Field(default_factory=list)


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
    # 分级削减扩展（缺省值保证旧记录/旧调用兼容）
    level_before: int = 0
    level_after: int = 0
    plan: ShedPlan | None = None


def parse_controllable_loads(
    entries: tuple[str, ...] | list[str],
) -> tuple[list[ControllableLoad], list[str]]:
    """解析 `id:priority:sheddable_kw` 配置项。

    返回 (有效负载列表, 非法原始项列表)。有效列表按 (priority, device_id)
    升序排序，即削减顺序；非法项原样保留以便对外给出可区分结果。
    """
    loads: list[ControllableLoad] = []
    invalid: list[str] = []
    for raw in entries:
        parts = [p.strip() for p in raw.split(":")]
        try:
            if len(parts) != 3 or not parts[0]:
                raise ValueError("expect id:priority:sheddable_kw")
            load = ControllableLoad(
                device_id=parts[0],
                priority=int(parts[1]),
                sheddable_kw=float(parts[2]),
            )
            if load.sheddable_kw <= 0:
                raise ValueError("sheddable_kw must be > 0")
            loads.append(load)
        except ValueError:
            invalid.append(raw)
    loads.sort(key=lambda l: (l.priority, l.device_id))
    return loads, invalid


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
