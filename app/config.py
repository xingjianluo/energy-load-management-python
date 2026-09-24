"""配置项（骨架）。所有阈值均可通过环境变量覆盖。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """运行配置：阈值、滞回、时间容忍度等。"""

    # 遥测时间容忍度
    max_future_skew_seconds: float = 60.0
    max_stale_seconds: float = 3600.0
    # 负载调度阈值（kW）与滞回
    shed_threshold_kw: float = 10.0
    restore_threshold_kw: float = 8.0
    # 储能低电量阈值（0-1）
    storage_low_soc: float = 0.2
    # 持久化文件（None 表示纯内存）
    store_path: str | None = None


def load_settings() -> Settings:
    """从环境变量加载配置（骨架实现，后续补全）。"""
    return Settings()
