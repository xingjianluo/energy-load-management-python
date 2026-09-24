"""配置项：所有阈值均可通过环境变量覆盖（前缀 ELM_）。"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """运行配置：阈值、滞回、时间容忍度等。"""

    # 遥测时间容忍度
    max_future_skew_seconds: float = 60.0
    max_stale_seconds: float = 3600.0
    # 负载调度阈值（kW）与滞回：触发限载 > shed，解除限载 < restore
    shed_threshold_kw: float = 10.0
    restore_threshold_kw: float = 8.0
    # 储能低电量阈值（0-1），低于该值时禁止解除限载
    storage_low_soc: float = 0.2
    # 持久化文件（None 表示纯内存）
    store_path: str | None = None
    # 已知设备注册表（逗号分隔）；空表示不启用设备白名单
    known_devices: tuple[str, ...] = ()


_ENV_MAP = {
    "max_future_skew_seconds": ("ELM_MAX_FUTURE_SKEW_SECONDS", float),
    "max_stale_seconds": ("ELM_MAX_STALE_SECONDS", float),
    "shed_threshold_kw": ("ELM_SHED_THRESHOLD_KW", float),
    "restore_threshold_kw": ("ELM_RESTORE_THRESHOLD_KW", float),
    "storage_low_soc": ("ELM_STORAGE_LOW_SOC", float),
    "store_path": ("ELM_STORE_PATH", str),
}


def load_settings(env: dict[str, str] | None = None) -> Settings:
    """从环境变量加载配置；缺省使用默认值。"""
    env = os.environ if env is None else env
    kwargs: dict[str, object] = {}
    for field, (name, cast) in _ENV_MAP.items():
        raw = env.get(name)
        if raw is not None and raw != "":
            kwargs[field] = cast(raw)
    devices = env.get("ELM_KNOWN_DEVICES", "")
    if devices:
        kwargs["known_devices"] = tuple(
            d.strip() for d in devices.split(",") if d.strip()
        )
    return Settings(**kwargs)  # type: ignore[arg-type]
