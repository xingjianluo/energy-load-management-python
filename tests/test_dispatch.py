"""负载调度：阈值边界、滞回不抖动、冲突命令确定性、决策可追溯。"""
from __future__ import annotations

import threading

from app.config import Settings
from app.dispatch import LoadDispatcher
from app.models import LoadAction, SystemMode, SystemSnapshot

from tests.conftest import log_case


def snap(net: float, soc: float = 0.5) -> SystemSnapshot:
    return SystemSnapshot(
        load_kw=net, generation_kw=0.0, storage_soc=soc, net_load_kw=net
    )


def make_dispatcher(settings: Settings | None = None) -> LoadDispatcher:
    return LoadDispatcher(settings or Settings())


def test_threshold_boundary_exact_values():
    d = make_dispatcher(Settings(shed_threshold_kw=10.0, restore_threshold_kw=8.0))
    # 恰好等于触发阈值：不触发（严格大于）
    r = d.evaluate(snap(10.0))
    log_case("boundary-at-shed", {"net": 10.0}, r.model_dump())
    assert r.action == LoadAction.NONE and d.mode == SystemMode.NORMAL
    # 恰好超过
    r = d.evaluate(snap(10.001))
    log_case("boundary-above-shed", {"net": 10.001}, r.model_dump())
    assert r.action == LoadAction.SHED and d.mode == SystemMode.SHEDDING
    # 恰好等于解除阈值：不解除（严格小于）
    r = d.evaluate(snap(8.0))
    log_case("boundary-at-restore", {"net": 8.0}, r.model_dump())
    assert r.action == LoadAction.NONE and d.mode == SystemMode.SHEDDING
    # 恰好低于解除阈值
    r = d.evaluate(snap(7.999))
    log_case("boundary-below-restore", {"net": 7.999}, r.model_dump())
    assert r.action == LoadAction.RESTORE and d.mode == SystemMode.NORMAL


def test_hysteresis_no_flapping():
    d = make_dispatcher()
    d.evaluate(snap(11.0))  # 触发限载
    assert d.mode == SystemMode.SHEDDING
    # 在滞回带内来回波动：模式保持不变
    for net in (9.5, 8.5, 9.9, 8.001):
        r = d.evaluate(snap(net))
        log_case("hysteresis-band", {"net": net}, r.model_dump())
        assert r.action == LoadAction.NONE
        assert d.mode == SystemMode.SHEDDING
    r = d.evaluate(snap(7.0))
    assert r.action == LoadAction.RESTORE


def test_low_soc_blocks_restore():
    d = make_dispatcher()
    r = d.evaluate(snap(1.0, soc=0.1))  # 低电量触发限载
    log_case("low-soc-shed", {"net": 1.0, "soc": 0.1}, r.model_dump())
    assert r.action == LoadAction.SHED
    # 负载已低但电量未恢复：不得解除（避免抖动）
    r = d.evaluate(snap(1.0, soc=0.15))
    log_case("low-soc-hold", {"net": 1.0, "soc": 0.15}, r.model_dump())
    assert r.action == LoadAction.NONE and d.mode == SystemMode.SHEDDING
    r = d.evaluate(snap(1.0, soc=0.5))
    assert r.action == LoadAction.RESTORE


def test_decision_traceability():
    d = make_dispatcher()
    s = snap(12.0, soc=0.9)
    r = d.evaluate(s)
    log_case("traceable-decision", s.model_dump(), r.model_dump())
    assert r.snapshot.net_load_kw == 12.0  # 决策可追溯到输入快照
    assert r.thresholds["shed_threshold_kw"] == 10.0  # 与判定依据一致
    assert any("shed_threshold" in reason for reason in r.reasons)
    assert r.mode_before == SystemMode.NORMAL and r.mode_after == SystemMode.SHEDDING
    assert d.decisions()[-1].decision_id == r.decision_id


def test_conflicting_commands_single_outcome():
    """并发 shed/restore 冲突命令：串行裁决，只有一个确定结果。"""
    d = make_dispatcher()
    results: list[LoadAction] = []
    errors: list[Exception] = []

    def run(cmd: str) -> None:
        try:
            results.append(d.command(cmd, snap(5.0)).action)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(c,))
               for c in ["shed", "restore"] * 10]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log_case("concurrent-commands", "shed/restore x10",
             {"actions": [a.value for a in results], "final_mode": d.mode})
    assert not errors
    assert len(results) == 20
    # 真实迁移次数与最终模式一致：迁移序列严格交替，绝无并发撕裂
    transitions = [a for a in results if a != LoadAction.NONE]
    assert all(a != b for a, b in zip(transitions, transitions[1:]))
    # 历史记录与最终状态一致
    assert d.decisions()[-1].mode_after == d.mode


def test_mode_persists_across_restart(tmp_path):
    settings = Settings(store_path=str(tmp_path / "events.jsonl"))
    d1 = make_dispatcher(settings)
    d1.evaluate(snap(11.0))
    assert d1.mode == SystemMode.SHEDDING
    d2 = make_dispatcher(settings)  # 模拟重启
    log_case("restart-recovery", settings.store_path,
             {"mode_before": "shedding", "mode_after_restart": d2.mode})
    assert d2.mode == SystemMode.SHEDDING  # 模式恢复
    assert len(d2.decisions()) == 1  # 决策历史不重复
