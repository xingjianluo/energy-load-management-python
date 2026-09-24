"""领域对象到 API JSON 结构的序列化；未知字段不会出现在固定输出中，
但事件 raw payload 保留（前向兼容：新指标/字段不破坏既有查询）。"""

from energy.devices import DeviceSpec
from energy.models import (
    CommandResult,
    Decision,
    Event,
    MetricReading,
    SystemSnapshot,
)


def reading_out(r: MetricReading) -> dict:
    return {
        "value": r.value,
        "unit": r.unit,
        "ts": r.ts.isoformat(),
        "event_id": r.event_id,
    }


def snapshot_out(snap: SystemSnapshot) -> dict:
    return {
        "generated_at": snap.generated_at.isoformat(),
        "battery_soc": snap.battery_soc,
        "solar_generation_w": snap.solar_generation_w,
        "load_power_w": snap.load_power_w,
        "net_load_w": snap.net_load_w,
        "devices": {
            device_id: {
                "device_type": ds.device_type.value,
                "readings": {m: reading_out(r) for m, r in ds.readings.items()},
            }
            for device_id, ds in snap.devices.items()
        },
    }


def decision_out(d: Decision) -> dict:
    return {
        "decision_id": d.decision_id,
        "decided_at": d.decided_at.isoformat(),
        "curtailed": d.curtailed,
        "limit_w": d.limit_w,
        "trigger": d.trigger,
        "reasons": d.reasons,
        "input_snapshot": d.snapshot,
        "thresholds": d.thresholds,
    }


def command_result_out(r: CommandResult) -> dict:
    return {
        "command_id": r.command_id,
        "status": r.status.value,
        "decision_id": r.decision_id,
        "reason": r.reason,
        "applied_at": r.applied_at.isoformat() if r.applied_at else None,
    }


def event_out(e: Event) -> dict:
    return {
        "seq": e.seq,
        "event_id": e.event_id,
        "kind": e.kind.value,
        "received_at": e.received_at.isoformat(),
        "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
        "device_id": e.device_id,
        "metric": e.metric,
        "value": e.value,
        "unit": e.unit,
        "dedupe_key": e.dedupe_key,
        "payload": e.payload,
    }


def device_out(spec: DeviceSpec) -> dict:
    return {
        "device_id": spec.device_id,
        "device_type": spec.device_type.value,
        "controllable": spec.controllable,
        "metrics": {
            name: {
                "unit": m.unit,
                "min_value": m.min_value,
                "max_value": m.max_value,
            }
            for name, m in spec.metrics.items()
        },
    }
