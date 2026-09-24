"""遥测接入：重复、乱序、过期、未来、未知设备、非法单位、缺失字段。"""
from __future__ import annotations

from datetime import timedelta

from app.models import IngestStatus, RejectReason

from tests.conftest import log_case, make_payload


def test_accept_and_snapshot(svc, now):
    p = make_payload("e1", value=5.0)
    r = svc.ingest(p)
    log_case("accept", p, r.model_dump())
    assert r.status == IngestStatus.ACCEPTED
    snap = svc.snapshot()
    log_case("snapshot-after-accept", p, snap.model_dump())
    assert snap.load_kw == 5.0


def test_duplicate_is_idempotent(svc):
    p = make_payload("e1", value=5.0)
    first = svc.ingest(p)
    second = svc.ingest(p)
    log_case("duplicate", p, {"first": first.model_dump(), "second": second.model_dump()})
    assert first.status == IngestStatus.ACCEPTED
    assert second.status == IngestStatus.DUPLICATE
    assert len(svc.history()) == 1  # 重复提交不改变历史口径
    assert svc.snapshot().load_kw == 5.0  # 状态不变


def test_batch_resubmit_is_idempotent(svc, now):
    batch = [
        make_payload("b1", value=1.0),
        make_payload("b2", value=2.0, ts=now + timedelta(seconds=1)),
    ]
    r1 = svc.ingest_batch(batch)
    r2 = svc.ingest_batch(batch)
    log_case("batch-resubmit", batch, {"first": r1.model_dump(), "second": r2.model_dump()})
    assert (r1.accepted, r1.duplicates, r1.rejected) == (2, 0, 0)
    assert (r2.accepted, r2.duplicates, r2.rejected) == (0, 2, 0)
    assert len(svc.history()) == 2


def test_out_of_order_rejected(svc, now):
    svc.ingest(make_payload("e1", ts=now))
    p = make_payload("e2", ts=now - timedelta(seconds=5))
    r = svc.ingest(p)
    log_case("out-of-order", p, r.model_dump())
    assert r.status == IngestStatus.REJECTED
    assert r.reason == RejectReason.OUT_OF_ORDER
    assert len(svc.history()) == 1


def test_stale_and_future_rejected(svc, now):
    p_old = make_payload("e1", ts=now - timedelta(hours=2))
    p_new = make_payload("e2", ts=now + timedelta(minutes=5))
    r_old, r_new = svc.ingest(p_old), svc.ingest(p_new)
    log_case("stale", p_old, r_old.model_dump())
    log_case("future", p_new, r_new.model_dump())
    assert r_old.reason == RejectReason.STALE_TIMESTAMP
    assert r_new.reason == RejectReason.FUTURE_TIMESTAMP
    assert svc.history() == []


def test_unknown_device_metric_unit(svc):
    p1 = make_payload("e1", device="ghost")
    p2 = make_payload("e2", metric="flux_capacitance")
    p3 = make_payload("e3", unit="V")  # power 不接受 V
    r1, r2, r3 = svc.ingest(p1), svc.ingest(p2), svc.ingest(p3)
    log_case("unknown-device", p1, r1.model_dump())
    log_case("unknown-metric", p2, r2.model_dump())
    log_case("invalid-unit", p3, r3.model_dump())
    assert r1.reason == RejectReason.UNKNOWN_DEVICE
    assert r2.reason == RejectReason.UNKNOWN_METRIC
    assert r3.reason == RejectReason.INVALID_UNIT
    assert svc.history() == []


def test_missing_and_malformed_fields(svc):
    cases = [
        {"event_id": "e1"},  # 缺失字段
        make_payload("e2", value="hot"),  # 非法数值
        make_payload("e3", timestamp="not-a-time"),  # 非法时间
        "not-an-object",  # 非对象
    ]
    for p in cases:
        r = svc.ingest(p)
        log_case("malformed", p, r.model_dump())
        assert r.status == IngestStatus.REJECTED
    assert svc.history() == []


def test_unit_normalization(svc, now):
    p = make_payload("e1", value=1500, unit="W")
    r = svc.ingest(p)
    log_case("unit-normalization", p, r.model_dump())
    assert r.status == IngestStatus.ACCEPTED
    assert abs(svc.snapshot().load_kw - 1.5) < 1e-9


def test_unknown_extra_fields_tolerated(svc, now):
    p = make_payload("e1", value=3.0, firmware="v9", vendor={"x": 1})
    r = svc.ingest(p)
    log_case("extra-fields", p, r.model_dump())
    assert r.status == IngestStatus.ACCEPTED
    ev = svc.history()[0]
    assert ev["firmware"] == "v9" and ev["vendor"] == {"x": 1}  # 未知字段原样保留
