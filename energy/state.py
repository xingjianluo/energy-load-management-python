"""状态派生引擎。

唯一事实来源是事件日志；当前状态通过重放已接受的遥测事件得到：
- 每个 (设备, 指标) 保留"已接受且事件时间戳最新"的读数
  （同时间戳时以 seq 较大者为准，保证重放与实时口径一致）；
- 系统级汇总量在快照时按设备类型求和/选取；
- 命令/决策事件不改变物理读数（它们单独记录）。

因此重启后重放日志即可得到完全一致的状态；拒绝事件从未入库，
天然不会污染状态，也不会重复计量。
"""

import logging
from dataclasses import dataclass

from energy.config import Settings
from energy.devices import DeviceRegistry
from energy.models import (
    DeviceSnapshot,
    DeviceType,
    Event,
    EventKind,
    MetricReading,
    SystemSnapshot,
)

logger = logging.getLogger("energy.state")


@dataclass(frozen=True)
class _Slot:
    reading: MetricReading
    seq: int


class StateEngine:
    def __init__(self, settings: Settings, registry: DeviceRegistry) -> None:
        self._settings = settings
        self._registry = registry
        self._slots: dict[tuple[str, str], _Slot] = {}

    def apply(self, event: Event) -> None:
        """纳入一个事件；只接受遥测事件。"""
        if event.kind is not EventKind.TELEMETRY:
            return
        if not event.device_id or not event.metric or event.occurred_at is None:
            return
        key = (event.device_id, event.metric)
        prev = self._slots.get(key)
        reading = MetricReading(
            device_id=event.device_id,
            metric=event.metric,
            value=float(event.value) if event.value is not None else 0.0,
            unit=event.unit or "",
            ts=event.occurred_at,
            event_id=event.event_id,
        )
        if prev is None:
            take = True
        elif reading.ts > prev.reading.ts:
            take = True
        elif reading.ts == prev.reading.ts and event.seq > prev.seq:
            take = True
        else:
            take = False
        if take:
            self._slots[key] = _Slot(reading=reading, seq=event.seq)

    def replay(self, events: list[Event]) -> None:
        """按 seq 顺序全量重放。"""
        for event in sorted(events, key=lambda e: e.seq):
            self.apply(event)

    # ------------------------------------------------------------------
    def _latest(self, device_id: str, metric: str) -> MetricReading | None:
        slot = self._slots.get((device_id, metric))
        return slot.reading if slot else None

    def _sum_by_type(self, device_type: DeviceType, metric: str) -> float | None:
        total = 0.0
        found = False
        for spec in self._registry.list_devices():
            if spec.device_type is not device_type:
                continue
            reading = self._latest(spec.device_id, metric)
            if reading is not None:
                total += reading.value
                found = True
        return total if found else None

    def snapshot(self, now: datetime) -> SystemSnapshot:
        devices: dict[str, DeviceSnapshot] = {}
        battery_soc: float | None = None
        # 发电：solar 各设备 power_w 之和
        solar_generation_w = self._sum_by_type(DeviceType.SOLAR, "power_w")
        # 用电：所有负载 power_w 之和
        load_power_w = self._sum_by_type(DeviceType.LOAD, "power_w")
        # 电池功率（正充电/负放电）
        battery_power_w = self._sum_by_type(DeviceType.BATTERY, "power_w")

        for spec in self._registry.list_devices():
            snap = DeviceSnapshot(device_id=spec.device_id, device_type=spec.device_type)
            for metric_name in spec.metrics:
                reading = self._latest(spec.device_id, metric_name)
                if reading is not None:
                    snap.readings[metric_name] = reading
            devices[spec.device_id] = snap
            if spec.device_type is DeviceType.BATTERY:
                soc_reading = self._latest(spec.device_id, "soc")
                if soc_reading is not None:
                    battery_soc = soc_reading.value

        # 净负载 = 用电 - 发电 - 电池放电；放电时 battery_power_w 为负
        if load_power_w is None and solar_generation_w is None and battery_power_w is None:
            net_load_w = None
        else:
            net_load_w = (
                (load_power_w or 0.0)
                - (solar_generation_w or 0.0)
                - (-battery_power_w if battery_power_w is not None and battery_power_w < 0 else 0.0)
            )

        snap = SystemSnapshot(
            generated_at=now,
            devices=devices,
            battery_soc=battery_soc,
            solar_generation_w=solar_generation_w,
            load_power_w=load_power_w,
            net_load_w=net_load_w,
        )
        logger.info(
            "snapshot net_load_w=%s solar_w=%s load_w=%s battery_power_w=%s soc=%s",
            net_load_w,
            solar_generation_w,
            load_power_w,
            battery_power_w,
            battery_soc,
        )
        return snap
