"""数据模型与枚举定义（骨架）。"""
from __future__ import annotations

from enum import Enum


class IngestStatus(str, Enum):
    """单条遥测数据的处理结果。"""

    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"


class RejectReason(str, Enum):
    """可区分的拒绝原因。"""

    MISSING_FIELD = "missing_field"
    UNKNOWN_DEVICE = "unknown_device"
    INVALID_UNIT = "invalid_unit"
    INVALID_VALUE = "invalid_value"
    STALE_TIMESTAMP = "stale_timestamp"
    FUTURE_TIMESTAMP = "future_timestamp"
    OUT_OF_ORDER = "out_of_order"


class SystemMode(str, Enum):
    """系统运行模式（带滞回的状态机）。"""

    NORMAL = "normal"
    SHEDDING = "shedding"
