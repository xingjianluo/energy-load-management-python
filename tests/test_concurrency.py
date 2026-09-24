"""并发读写、幂等竞态、重启恢复一致性。"""
from __future__ import annotations

import threading
from datetime import timedelta

from app.config import Settings
from app.models import IngestStatus
from app.telemetry import TelemetryService

from tests.conftest import FIXED_NOW, log_case, make_payload


def test_concurrent_same_event_single_accept(svc):
    """同一 event_id 并发提交：恰好一个 accepted，其余 duplicate。"""
    p = make_payload("race-1", value=5.0)
    results = []

    def submit():
        results.append(svc.ingest(dict(p)).status)

    threads = [threading.Thread(target=submit) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log_case("concurrent-same-event", p, [s.value for s in results])
    assert results.count(IngestStatus.ACCEPTED) == 1
    assert results.count(IngestStatus.DUPLICATE) == 15
    assert len(svc.history()) == 1  # 只计量一次


def test_concurrent_mixed_load(svc, now):
    """并发读写混杂：不同事件 + 快照读取，最终状态确定。"""
    errors = []
    for n in range(4):
        svc.register_device(f"load-w{n}", "load")

    def writer(n: int):
        try:
            for i in range(20):
                svc.ingest(make_payload(
                    f"w{n}-{i}", device=f"load-w{n}", value=1.0,
                    ts=FIXED_NOW - timedelta(seconds=100 - i),
                ))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    def reader():
        try:
            for _ in range(50):
                svc.snapshot()
                svc.history()
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = ([threading.Thread(target=writer, args=(n,)) for n in range(4)]
               + [threading.Thread(target=reader) for _ in range(2)])
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(svc.history()) == 80
    snap = svc.snapshot()
    log_case("concurrent-mixed-final", "4 writers x 20 events", snap.model_dump())
    assert abs(snap.load_kw - 4.0) < 1e-9  # 4 台设备各 1.0kW，最新值确定


def test_restart_no_double_count(settings):
    """重启后：已接受事件不丢失、不重复计量，重复提交仍是 duplicate。"""
    clock = lambda: FIXED_NOW  # noqa: E731
    s1 = TelemetryService(settings, now=clock)
    s1.register_device("load1", "load")
    s1.ingest(make_payload("e1", value=5.0))
    s1.ingest(make_payload("e2", value=6.0,
                           ts=FIXED_NOW + timedelta(seconds=1)))

    s2 = TelemetryService(settings, now=clock)  # 模拟重启
    log_case("restart", settings.store_path,
             {"events": len(s2.history()), "devices": s2.list_devices()})
    assert len(s2.history()) == 2  # 不丢失
    assert s2.list_devices() == {"load1": "load"}  # 注册表恢复
    # 重启后重复提交同一事件：duplicate，不重复计量
    r = s2.ingest(make_payload("e1", value=5.0))
    assert r.status == IngestStatus.DUPLICATE
    assert len(s2.history()) == 2
    # 快照口径与重启前一致
    assert s2.snapshot().load_kw == 6.0
    # 乱序约束在重启后仍然生效
    r = s2.ingest(make_payload("e3", value=9.9,
                               ts=FIXED_NOW - timedelta(seconds=1)))
    assert r.status == IngestStatus.REJECTED


def test_corrupted_store_line_tolerated(settings):
    """存储文件含损坏行：其余事件正常恢复，不污染整体状态。"""
    clock = lambda: FIXED_NOW  # noqa: E731
    s1 = TelemetryService(settings, now=clock)
    s1.register_device("load1", "load")
    s1.ingest(make_payload("e1", value=5.0))
    with open(settings.store_path, "a", encoding="utf-8") as fh:
        fh.write('{"event_id": "broken", "truncated"')  # 截断行
    s2 = TelemetryService(settings, now=clock)
    log_case("corrupted-line", settings.store_path,
             {"recovered": len(s2.history())})
    assert len(s2.history()) == 1
    assert s2.snapshot().load_kw == 5.0
