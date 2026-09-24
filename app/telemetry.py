"""遥测接入与幂等处理（骨架）。"""
from __future__ import annotations

from typing import Any


class TelemetryService:
    """校验、去重、乱序/过期/未来时间戳判定。"""

    def ingest(self, payload: dict[str, Any]) -> dict[str, Any]:
        """处理单条遥测，返回稳定可区分的结果。"""
        raise NotImplementedError
