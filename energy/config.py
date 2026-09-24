"""配置项定义与环境变量加载。

所有阈值集中在此；环境变量前缀 ENERGY_，例如
ENERGY_CURTAIL_TRIGGER_W=6000、ENERGY_DATA_DIR=/var/lib/energy。
"""

import os
from dataclasses import dataclass

from energy.errors import ValidationAppError


@dataclass(frozen=True)
class Settings:
    # 持久化目录与事件日志文件名
    data_dir: str = ".data"
    event_log_file: str = "events.jsonl"
    # 时间戳校验容差（秒）：早于 now-past_tolerance 视为过期，晚于 now+future_tolerance 视为未来
    past_tolerance_seconds: float = 3600.0
    future_tolerance_seconds: float = 30.0
    # 净负载限载阈值（瓦）：高于 trigger 进入限载，低于 release 才解除（滞回）
    curtail_trigger_w: float = 5000.0
    curtail_release_w: float = 3000.0
    # 储能 SOC 阈值（0~1）：低于 low 加深限载，回升至 high 才解除该条触发
    soc_low: float = 0.2
    soc_high: float = 0.8
    # 限载时对可控负载施加的总功率上限（瓦）
    load_limit_w: float = 3000.0
    # 数值合法范围（通用护栏，指标自身有更具体范围时优先使用指标范围）
    value_abs_max: float = 1.0e9

    def validate(self) -> None:
        if self.past_tolerance_seconds < 0 or self.future_tolerance_seconds < 0:
            raise ValidationAppError("tolerance must be non-negative")
        if self.curtail_trigger_w <= self.curtail_release_w:
            raise ValidationAppError("curtail_trigger_w must be greater than curtail_release_w")
        if not (0.0 <= self.soc_low < self.soc_high <= 1.0):
            raise ValidationAppError("require 0 <= soc_low < soc_high <= 1")
        if self.load_limit_w < 0:
            raise ValidationAppError("load_limit_w must be non-negative")


_FLOAT_FIELDS = (
    "past_tolerance_seconds",
    "future_tolerance_seconds",
    "curtail_trigger_w",
    "curtail_release_w",
    "soc_low",
    "soc_high",
    "load_limit_w",
    "value_abs_max",
)
_STR_FIELDS = ("data_dir", "event_log_file")


def load_settings(overrides: dict[str, object] | None = None) -> Settings:
    """从 ENERGY_<FIELD_UPPER> 环境变量读取配置，并做一致性校验。

    overrides 主要供测试使用，优先级高于环境变量。
    """

    raw: dict[str, object] = {}
    for name in _FLOAT_FIELDS:
        env = os.environ.get(f"ENERGY_{name.upper()}")
        if env is not None:
            try:
                raw[name] = float(env)
            except ValueError as exc:
                raise ValidationAppError(
                    f"invalid float for {name}: {env!r}"
                ) from exc
    for name in _STR_FIELDS:
        env = os.environ.get(f"ENERGY_{name.upper()}")
        if env is not None:
            raw[name] = env
    if overrides:
        raw.update(overrides)

    try:
        settings = Settings(**raw)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ValidationAppError(f"unknown config field: {exc}") from exc
    settings.validate()
    return settings
