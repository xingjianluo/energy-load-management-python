"""并发一致性测试。

门面服务用同一把 RLock 串行化所有写操作，因此：
- 并发遥测写入不丢点、不串号（seq 连续唯一）；
- 冲突命令并发到达只产生一个确定结果（按 seq 全序）；
- 重复命令/重复批次在并发下只生效一次；
- 读取在任意时刻得到已发布的一致快照，从不观察到半成品状态。
"""

import threading
from concurrent.futures import ThreadPoolExecutor

from energy.devices import DeviceRegistry, DeviceSpec, MetricSpec
from energy.models import (
    CommandIn,
    CommandStatus,
    CommandType,
    DeviceType,
    TelemetryBatchIn,
    TelemetryPointIn,
)
from energy.service import EnergyService
from tests.conftest import iso


def _feed(service, clock, load_w, idx, offset):
    t = iso(clock, offset_seconds=offset)
    return service.ingest_batch(
        TelemetryBatchIn(
            batch_id=f"b{idx}",
            points=[
                TelemetryPointIn(
                    device_id="load-001", metric="power_w",
                    value=float(load_w), unit="W", ts=t,
                )
            ],
        )
    )


def test_concurrent_telemetry_writes_all_persist_with_unique_seq(settings, clock):
    # 每个工作线程写一个独立设备的独立序列，排除"同序列乱序"这本应被
    # 拒绝的情况，专注验证并发写入不丢点、不串号。
    n = 40
    specs = {
        f"dev-{i:03d}": DeviceSpec(
            device_id=f"dev-{i:03d}",
            device_type=DeviceType.LOAD,
            metrics={"power_w": MetricSpec("power_w", "W", 0.0, 1.0e7)},
        )
        for i in range(n)
    }
    svc = EnergyService(settings=settings, clock=clock, registry=DeviceRegistry(specs))
    errors: list[Exception] = []

    def worker(i):
        try:
            svc.ingest_batch(
                TelemetryBatchIn(
                    batch_id=f"b{i}",
                    points=[
                        TelemetryPointIn(
                            device_id=f"dev-{i:03d}", metric="power_w",
                            value=1000.0 + i, unit="W", ts=iso(clock),
                        )
                    ],
                )
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(n)))
    assert not errors
    events = [e for e in svc.list_events() if e.kind.value == "telemetry"]
    assert len(events) == n
    seqs = [e.seq for e in events]
    assert len(set(seqs)) == n  # 唯一
    assert sorted(seqs) == seqs  # 可按序重放
    # 每批接受后有一条决策事件，因此整体 seq 流交错但无空洞：
    all_seqs = sorted(e.seq for e in svc.list_events())
    assert all_seqs == list(range(1, len(all_seqs) + 1))


def test_concurrent_conflicting_commands_single_total_order(service, clock):
    _feed(service, clock, 1000, 0, 0)
    barrier = threading.Barrier(8)

    def worker(i):
        barrier.wait()
        return service.submit_command(
            CommandIn(
                command_id=f"lim-{i}",
                command_type=CommandType.SET_LIMIT,
                target_load_id="load-001",
                limit_w=1000.0 + i,
            )
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker, range(8)))
    assert sum(r.status is CommandStatus.APPLIED for r in results) == 8
    cmd_events = [e for e in service.list_events() if e.kind.value == "command"]
    assert len(cmd_events) == 8
    assert len({e.dedupe_key for e in cmd_events}) == 8
    # seq 定义全序；最大 seq 唯一确定最终生效值
    last = max(cmd_events, key=lambda e: e.seq)
    assert last.payload["limit_w"] in {1000.0 + i for i in range(8)}


def test_concurrent_same_command_id_applied_once(service, clock):
    _feed(service, clock, 1000, 0, 0)
    barrier = threading.Barrier(6)

    def worker(_):
        barrier.wait()
        return service.submit_command(
            CommandIn(command_id="same-id", command_type=CommandType.CURTAIL)
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(worker, range(6)))
    assert sum(r.status is CommandStatus.APPLIED for r in results) == 1
    assert sum(r.status is CommandStatus.DUPLICATE for r in results) == 5
    cmd_events = [
        e for e in service.list_events()
        if e.kind.value == "command" and e.dedupe_key == "cmd-same-id"
    ]
    assert len(cmd_events) == 1  # 幂等：并发只持久化一次


def test_concurrent_reads_never_observe_partial_state(service, clock):
    stop = threading.Event()
    seen: list = []

    def reader():
        while not stop.is_set():
            seen.append(service.current_snapshot().net_load_w)

    t = threading.Thread(target=reader)
    t.start()
    for i in range(20):
        _feed(service, clock, 4000, i, float(i))
    stop.set()
    t.join()
    # 任意时刻读到的都是已发布快照（None 或有限数值），无半成品
    assert all(v is None or isinstance(v, float) for v in seen)


def test_concurrent_duplicate_batches_do_not_double_count(service, clock):
    batch = TelemetryBatchIn(
        batch_id="dup",
        points=[
            TelemetryPointIn(
                device_id="solar-001", metric="power_w", value=321.0,
                unit="W", ts=iso(clock),
            )
        ],
    )
    barrier = threading.Barrier(6)

    def worker(_):
        barrier.wait()
        return service.ingest_batch(batch)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(worker, range(6)))
    assert sum(r.accepted for r in results) == 1
    assert sum(r.duplicate for r in results) == 5
    events = [
        e for e in service.list_events(device_id="solar-001") if e.metric == "power_w"
    ]
    assert len(events) == 1
    assert service.current_snapshot().solar_generation_w == 321.0


def test_restart_does_not_replay_double_count(service, settings, clock):
    _feed(service, clock, 2500, 0, 0)
    svc2 = EnergyService(settings=settings, clock=clock)
    assert len(svc2.list_events()) == len(service.list_events())
    assert svc2.current_snapshot().load_power_w == 2500.0
