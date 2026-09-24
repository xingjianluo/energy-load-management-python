"""设备与指标注册表。

遥测的合法性首先由注册表裁定：设备是否已知、指标是否属于该设备、
单位是否为该指标规定的标准单位、数值是否落在指标声明的范围内。
未知设备/指标/非法单位一律拒绝（不做静默兼容）。
"""

from dataclasses import dataclass

from energy.models import DeviceType

_POWER_MAX = 1.0e7


@dataclass(frozen=True)
class MetricSpec:
    metric: str
    unit: str  # 唯一合法的标准单位，提交其他单位即拒绝
    min_value: float
    max_value: float


@dataclass(frozen=True)
class DeviceSpec:
    device_id: str
    device_type: DeviceType
    metrics: dict[str, MetricSpec]
    controllable: bool = False  # 是否为可接受调度命令的可控负载


def _power(metric: str = "power_w") -> MetricSpec:
    # 功率：瓦。发电/用电为非负；电池允许负值（放电）
    return MetricSpec(metric, "W", 0.0, _POWER_MAX)


def build_default_registry() -> DeviceRegistry:
    devices = [
        DeviceSpec(
            device_id="battery-001",
            device_type=DeviceType.BATTERY,
            metrics={
                "soc": MetricSpec("soc", "fraction", 0.0, 1.0),
                # 正值=充电（吸收功率），负值=放电（释放功率）
                "power_w": MetricSpec("power_w", "W", -_POWER_MAX, _POWER_MAX),
            },
        ),
        DeviceSpec(
            device_id="solar-001",
            device_type=DeviceType.SOLAR,
            metrics={"power_w": _power()},
        ),
        DeviceSpec(
            device_id="grid-001",
            device_type=DeviceType.GRID,
            # 正值=从电网取电，负值=向电网反送
            metrics={"power_w": MetricSpec("power_w", "W", -_POWER_MAX, _POWER_MAX)},
        ),
        DeviceSpec(
            device_id="load-001",
            device_type=DeviceType.LOAD,
            metrics={"power_w": _power()},
            controllable=True,
        ),
        DeviceSpec(
            device_id="load-002",
            device_type=DeviceType.LOAD,
            metrics={"power_w": _power()},
            controllable=False,
        ),
    ]
    return DeviceRegistry({d.device_id: d for d in devices})


class DeviceRegistry:
    def __init__(self, devices: dict[str, DeviceSpec] | None = None) -> None:
        self._devices: dict[str, DeviceSpec] = dict(devices or {})

    def get(self, device_id: str) -> DeviceSpec | None:
        return self._devices.get(device_id)

    def list_devices(self) -> list[DeviceSpec]:
        return sorted(self._devices.values(), key=lambda d: d.device_id)

    def controllable_loads(self) -> list[DeviceSpec]:
        return [
            d
            for d in self.list_devices()
            if d.device_type == DeviceType.LOAD and d.controllable
        ]
