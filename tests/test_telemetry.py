"""遥测接入测试：重复/乱序/异常输入的稳定可区分结论与幂等保证。"""

import math

import pytest

from energy.models import (
    PointStatus,
    RejectReason,
    TelemetryBatchIn,
    TelemetryPointIn,
)
from tests.conftest import iso


def _point(clock, **over):
    base = dict(
        device_id="battery-001",
        metric="soc",
        value=0.5,
        unit="fraction",
        ts=iso(clock),
    )
    base.update(over)
    return TelemetryPointIn(**base)


def _batch(clock, *points, batch_id=None):
    return TelemetryBatchIn(points=list(points), batch_id=batch_id)


def test_happy_path_accepts_and_persists(service, clock):
    res = service.ingest_batch(_batch(clock, _point(clock)))
    assert res.accepted == 1
    assert res.results[0].status is PointStatus.ACCEPTED
    assert service.current_snapshot().battery_soc == 0.5


def test_unknown_device_rejected(service, clock):
    res = service.ingest_batch(_batch(clock, _point(clock, device_id="ghost-9")))
    assert res.rejected == 1
    assert res.results[0].reason == RejectReason.UNKNOWN_DEVICE.value


def test_unknown_metric_rejected(service, clock):
    res = service.ingest_batch(_batch(clock, _point(clock, metric="temperature")))
    assert res.results[0].reason == RejectReason.UNKNOWN_METRIC.value


def test_illegal_unit_rejected(service, clock):
    res = service.ingest_batch(_batch(clock, _point(clock, unit="percent")))
    assert res.results[0].reason == RejectReason.ILLEGAL_UNIT.value


def test_value_out_of_range_rejected(service, clock):
    res = service.ingest_batch(_batch(clock, _point(clock, value=1.5)))
    assert res.results[0].reason == RejectReason.VALUE_OUT_OF_RANGE.value
    res2 = service.ingest_batch(_batch(clock, _point(clock, value=-0.01)))
    assert res2.results[0].reason == RejectReason.VALUE_OUT_OF_RANGE.value


def test_nan_and_infinity_rejected(service, clock):
    for bad in (math.nan, math.inf, -math.inf):
        res = service.ingest_batch(_batch(clock, _point(clock, value=bad)))
        assert res.results[0].reason == RejectReason.MALFORMED_VALUE.value


def test_missing_and_blank_fields_rejected(service, clock):
    res = service.ingest_batch(_batch(clock, _point(clock, device_id="  ")))
    assert res.results[0].reason == RejectReason.MISSING_FIELD.value
    res = service.ingest_batch(_batch(clock, _point(clock, ts="")))
    assert res.results[0].reason == RejectReason.MISSING_FIELD.value


def test_stale_timestamp_rejected(service, clock):
    old = iso(clock, offset_seconds=-3601)
    res = service.ingest_batch(_batch(clock, _point(clock, ts=old)))
    assert res.results[0].reason == RejectReason.STALE_TIMESTAMP.value


def test_future_timestamp_rejected(service, clock):
    future = iso(clock, offset_seconds=60)
    res = service.ingest_batch(_batch(clock, _point(clock, ts=future)))
    assert res.results[0].reason == RejectReason.FUTURE_TIMESTAMP.value


def test_naive_timestamp_rejected(service, clock):
    res = service.ingest_batch(
        _batch(clock, _point(clock, ts="2026-09-24T00:00:00"))
    )
    assert res.results[0].reason == RejectReason.BAD_TIMESTAMP.value


def test_out_of_order_rejected(service, clock):
    t0 = iso(clock)
    clock.advance(10)
    t1 = iso(clock)
    # 先接受 t1
    r1 = service.ingest_batch(_batch(clock, _point(clock, value=0.6, ts=t1)))
    assert r1.results[0].status is PointStatus.ACCEPTED
    # 再补 t0（更早）-> 乱序拒绝
    r2 = service.ingest_batch(_batch(clock, _point(clock, value=0.4, ts=t0)))
    assert r2.results[0].status is PointStatus.REJECTED
    assert r2.results[0].reason == RejectReason.OUT_OF_ORDER.value
    # 状态仍保持 t1 的值，未被旧数据覆盖
    assert service.current_snapshot().battery_soc == 0.6


def test_duplicate_resubmission_is_idempotent(service, clock):
    batch = _batch(clock, _point(clock), batch_id="b1")
    r1 = service.ingest_batch(batch)
    assert r1.accepted == 1
    events_before = len(service.list_events())
    # 同一批原样重复提交
    r2 = service.ingest_batch(batch)
    assert r2.accepted == 0
    assert r2.duplicate == 1
    assert r2.results[0].status is PointStatus.DUPLICATE
    assert r2.results[0].event_id == r1.results[0].event_id
    assert len(service.list_events()) == events_before  # 无新事件
    assert service.current_snapshot().battery_soc == 0.5


def test_client_event_id_dedupes_even_if_other_fields_differ(service, clock):
    t = iso(clock)
    p1 = TelemetryPointIn(
        device_id="battery-001", metric="soc", value=0.5,
        unit="fraction", ts=t, client_event_id="fixed-id",
    )
    p2 = TelemetryPointIn(
        device_id="battery-001", metric="soc", value=0.7,
        unit="fraction", ts=t, client_event_id="fixed-id",
    )
    r1 = service.ingest_batch(_batch(clock, p1))
    r2 = service.ingest_batch(_batch(clock, p2))
    assert r1.results[0].status is PointStatus.ACCEPTED
    assert r2.results[0].status is PointStatus.DUPLICATE
    # 第二个点的"新值"未被采用
    assert service.current_snapshot().battery_soc == 0.5


def test_partial_batch_rejection_does_not_poison_others(service, clock):
    t = iso(clock)
    good = _point(clock, value=0.5)
    bad_device = _point(clock, device_id="x")
    bad_unit = _point(clock, unit="kW")
    res = service.ingest_batch(_batch(clock, good, bad_device, bad_unit))
    assert res.accepted == 1 and res.rejected == 2
    assert service.current_snapshot().battery_soc == 0.5


def test_empty_batch_rejected(service):
    from energy.errors import BatchEmptyAppError

    with pytest.raises(BatchEmptyAppError):
        service.ingest_batch(TelemetryBatchIn(points=[]))


def test_threshold_boundary_timestamps_accepted(service, clock):
    # 恰好等于容差边界不应被判为过期/未来
    edge_old = iso(clock, offset_seconds=-3600.0)
    r = service.ingest_batch(_batch(clock, _point(clock, value=0.51, ts=edge_old)))
    assert r.results[0].status is PointStatus.ACCEPTED
    edge_future = iso(clock, offset_seconds=30.0)
    r = service.ingest_batch(_batch(clock, _point(clock, value=0.52, ts=edge_future)))
    assert r.results[0].status is PointStatus.ACCEPTED


def test_same_timestamp_second_point_is_duplicate(service, clock):
    # 同设备/指标/值/单位/时间戳 -> 规范键相同
    r1 = service.ingest_batch(_batch(clock, _point(clock, value=0.5)))
    r2 = service.ingest_batch(_batch(clock, _point(clock, value=0.5)))
    assert r2.results[0].status is PointStatus.DUPLICATE
    assert r1.results[0].event_id == r2.results[0].event_id
