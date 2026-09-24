"""事件日志持久化与恢复测试。"""

import os

from energy.clock import FakeClock
from energy.config import Settings
from energy.models import EventKind
from energy.service import EnergyService
from energy.store import EventStore
from tests.conftest import iso


def _append(store, key):
    return store.append_new(
        event_id=f"id-{key}",
        kind=EventKind.TELEMETRY,
        occurred_at=None,
        device_id="d",
        metric="m",
        value=1.0,
        unit="W",
        dedupe_key=key,
        payload={},
    )


def test_torn_tail_is_truncated_not_replayed(settings, clock):
    store = EventStore(settings, clock)
    _append(store, "a")
    _append(store, "b")
    # 模拟崩溃时最后一条未写完整
    with open(store.path, "a", encoding="utf-8") as fh:
        fh.write('{"seq": 3, "kind": "telem')

    store2 = EventStore(settings, clock)
    ids = [e.event_id for e in store2.all_events()]
    assert ids == ["id-a", "id-b"]  # 不丢已确认、不接受未确认


def test_seq_continues_after_recovery(settings, clock):
    store = EventStore(settings, clock)
    _append(store, "a")
    store3 = EventStore(settings, clock)
    e = _append(store3, "c")
    assert e.seq == 2  # 从日志续号，不重复


def test_restart_state_and_dedupe_identical(settings, clock):
    svc1 = EnergyService(settings=settings, clock=clock)
    t = iso(clock)
    from energy.models import TelemetryBatchIn, TelemetryPointIn

    batch = TelemetryBatchIn(points=[
        TelemetryPointIn(device_id="load-001", metric="power_w", value=4200.0,
                         unit="W", ts=t),
        TelemetryPointIn(device_id="solar-001", metric="power_w", value=200.0,
                         unit="W", ts=t),
    ])
    r1 = svc1.ingest_batch(batch)
    n_events = len(svc1.list_events())

    svc2 = EnergyService(settings=settings, clock=clock)
    assert len(svc2.list_events()) == n_events  # 无重复计量
    snap = svc2.current_snapshot()
    assert snap.load_power_w == 4200.0
    assert snap.solar_generation_w == 200.0
    # 重放后重复提交仍被识别
    r2 = svc2.ingest_batch(batch)
    assert r2.duplicate == 2 and r2.accepted == 0
    assert len(svc2.list_events()) == n_events
    # event_id 稳定
    assert [x.event_id for x in r2.results] == [x.event_id for x in r1.results]


def test_data_dir_created_when_missing(data_dir, clock):
    target = os.path.join(data_dir, "nested", "dir")
    store = EventStore(Settings(data_dir=target), clock)
    _append(store, "x")
    assert os.path.exists(store.path)
