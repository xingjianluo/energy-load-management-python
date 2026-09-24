"""负载调度测试：阈值边界、滞回、手动覆盖/冲突、决策可追溯。"""

import pytest

from energy.models import (
    CommandIn,
    CommandStatus,
    CommandType,
    TelemetryBatchIn,
    TelemetryPointIn,
)
from tests.conftest import iso


def feed(service, clock, load_w, solar_w=0.0, battery_power=0.0, soc=0.9):
    t = iso(clock)
    pts = [
        TelemetryPointIn(device_id="load-001", metric="power_w", value=load_w, unit="W", ts=t),
        TelemetryPointIn(device_id="solar-001", metric="power_w", value=solar_w, unit="W", ts=t),
        TelemetryPointIn(device_id="battery-001", metric="power_w", value=battery_power, unit="W", ts=t),
        TelemetryPointIn(device_id="battery-001", metric="soc", value=soc, unit="fraction", ts=t),
    ]
    return service.ingest_batch(TelemetryBatchIn(points=pts))


def last_decision(service):
    return service.list_decisions()[-1]


def test_trigger_boundary_curtails_at_threshold(service, clock):
    feed(service, clock, load_w=4999.0)
    assert last_decision(service).curtailed is False
    clock.advance(10)
    feed(service, clock, load_w=5000.0)  # >= trigger
    assert last_decision(service).curtailed is True
    assert last_decision(service).limit_w == 3000.0


def test_hysteresis_holds_in_middle_band(service, clock):
    feed(service, clock, load_w=5000.0)
    assert last_decision(service).curtailed is True
    clock.advance(10)
    feed(service, clock, load_w=4999.0)  # 在 (3000,5000) 区间
    assert last_decision(service).curtailed is True
    clock.advance(10)
    feed(service, clock, load_w=3001.0)
    assert last_decision(service).curtailed is True


def test_release_boundary_clears_at_release_threshold(service, clock):
    feed(service, clock, load_w=5000.0)
    clock.advance(10)
    feed(service, clock, load_w=3001.0)
    assert last_decision(service).curtailed is True
    clock.advance(10)
    feed(service, clock, load_w=3000.0)  # <= release
    assert last_decision(service).curtailed is False


def test_no_flapping_around_release(service, clock):
    # 在 release 附近来回：已清除后，未越过 trigger 不应重新置位
    feed(service, clock, load_w=2000.0)
    assert last_decision(service).curtailed is False
    clock.advance(10)
    feed(service, clock, load_w=3000.0)
    assert last_decision(service).curtailed is False
    clock.advance(10)
    feed(service, clock, load_w=4000.0)
    assert last_decision(service).curtailed is False
    clock.advance(10)
    feed(service, clock, load_w=4999.0)
    assert last_decision(service).curtailed is False


def test_solar_offset_moves_net_load(service, clock):
    feed(service, clock, load_w=6000.0, solar_w=2000.0)  # net 4000
    assert last_decision(service).curtailed is False
    clock.advance(10)
    feed(service, clock, load_w=6000.0, solar_w=500.0)  # net 5500
    assert last_decision(service).curtailed is True


def test_battery_discharge_reduces_net_load(service, clock):
    # 电池放电 power_w=-1500 应从净负载中扣除
    feed(service, clock, load_w=6000.0, battery_power=-1500.0)  # net 4500
    snap = service.current_snapshot()
    assert snap.net_load_w == 4500.0
    assert last_decision(service).curtailed is False


def test_low_soc_hysteresis(service, clock):
    feed(service, clock, load_w=1000.0, soc=0.2)  # <= low
    assert last_decision(service).curtailed is True
    clock.advance(10)
    feed(service, clock, load_w=1000.0, soc=0.5)  # 中间区间保持
    assert last_decision(service).curtailed is True
    clock.advance(10)
    feed(service, clock, load_w=1000.0, soc=0.8)  # >= high 解除
    assert last_decision(service).curtailed is False


def test_manual_release_conflicts_while_trigger_holds(service, clock):
    feed(service, clock, load_w=6000.0)
    result = service.submit_command(
        CommandIn(command_id="r1", command_type=CommandType.RELEASE)
    )
    assert result.status is CommandStatus.CONFLICT
    assert "trigger" in (result.reason or "")
    # 状态没有被错误解除
    assert last_decision(service).curtailed is True


def test_manual_release_in_band_takes_effect_and_requires_recross(service, clock):
    feed(service, clock, load_w=5000.0)
    clock.advance(10)
    feed(service, clock, load_w=4000.0)  # 滞回区间，仍限载
    result = service.submit_command(
        CommandIn(command_id="r1", command_type=CommandType.RELEASE)
    )
    assert result.status is CommandStatus.APPLIED
    # 之后区间内数据不再自动限载，需重新越过 trigger
    clock.advance(10)
    feed(service, clock, load_w=4000.0)
    assert last_decision(service).curtailed is False
    clock.advance(10)
    feed(service, clock, load_w=4999.0)
    assert last_decision(service).curtailed is False
    clock.advance(10)
    feed(service, clock, load_w=5000.0)
    assert last_decision(service).curtailed is True


def test_manual_curtail_command(service, clock):
    feed(service, clock, load_w=1000.0)
    assert last_decision(service).curtailed is False
    result = service.submit_command(
        CommandIn(command_id="m1", command_type=CommandType.CURTAIL)
    )
    assert result.status is CommandStatus.APPLIED
    assert result.decision_id is not None
    assert last_decision(service).curtailed is True


def test_duplicate_command_is_idempotent(service, clock):
    feed(service, clock, load_w=1000.0)
    cmd = CommandIn(command_id="m1", command_type=CommandType.CURTAIL)
    r1 = service.submit_command(cmd)
    decisions_before = len(service.list_decisions())
    r2 = service.submit_command(cmd)
    assert r1.status is CommandStatus.APPLIED
    assert r2.status is CommandStatus.DUPLICATE
    assert r2.decision_id == r1.decision_id
    assert len(service.list_decisions()) == decisions_before


def test_set_limit_custom_value(service, clock):
    feed(service, clock, load_w=6000.0)
    assert last_decision(service).limit_w == 3000.0
    service.submit_command(
        CommandIn(command_id="l1", command_type=CommandType.SET_LIMIT, limit_w=1500.0)
    )
    assert last_decision(service).limit_w == 1500.0


def test_set_limit_unknown_load_rejected(service, clock):
    feed(service, clock, load_w=6000.0)
    result = service.submit_command(
        CommandIn(
            command_id="l2",
            command_type=CommandType.SET_LIMIT,
            target_load_id="ghost",
            limit_w=1000.0,
        )
    )
    assert result.status is CommandStatus.REJECTED


def test_decision_records_inputs_thresholds_and_reasons(service, clock):
    feed(service, clock, load_w=5000.0)
    d = last_decision(service)
    assert d.snapshot["net_load_w"] == 5000.0
    assert d.thresholds["curtail_trigger_w"] == 5000.0
    assert d.thresholds["curtail_release_w"] == 3000.0
    assert any("trigger" in r for r in d.reasons)


def test_decisions_persist_across_restart(service, settings, clock):
    svc1 = service  # fixture
    feed(svc1, clock, load_w=5000.0)
    from energy.service import EnergyService

    svc2 = EnergyService(settings=settings, clock=clock)
    assert svc2.list_decisions() == svc1.list_decisions()
    assert svc2.current_snapshot().net_load_w == 5000.0
    # 低 soc 锁存也恢复
    clock.advance(5)
    feed(svc1, clock, load_w=5000.0, soc=0.1)
    svc3 = EnergyService(settings=settings, clock=clock)
    # 锁存状态：净负载恢复到 release 以下后，soc 锁存仍应使限载保持
    clock.advance(5)
    feed(svc3, clock, load_w=1000.0, soc=0.1)
    assert svc3.list_decisions()[-1].curtailed is True
