"""分级削减：选择顺序、档位迁移与滞回、容量不足、并发、重启恢复、幂等评估。"""
from __future__ import annotations

import threading

from app.config import Settings
from app.dispatch import LoadDispatcher
from app.models import DecisionOutcome, LoadAction, SystemMode, SystemSnapshot

from tests.conftest import log_case

# 三个可控负载：ac 最不重要先削，hvac 次之，fridge 最重要最后削
LOADS = ("ac:1:3.0", "hvac:2:4.0", "fridge:3:2.0")  # 合计 9kW


def snap(net: float, soc: float = 0.5) -> SystemSnapshot:
    return SystemSnapshot(
        load_kw=net, generation_kw=0.0, storage_soc=soc, net_load_kw=net
    )


def make_dispatcher(settings: Settings | None = None) -> LoadDispatcher:
    return LoadDispatcher(settings or Settings(controllable_loads=LOADS))


def test_shed_order_by_priority_and_net_after_in_restore_band():
    """触发限载：按优先级升序选择，净负荷落进可解除区间（< restore）。"""
    d = make_dispatcher()  # shed=10, restore=8
    r = d.evaluate(snap(11.5))
    log_case("graded-shed-order", {"net": 11.5}, r.model_dump())
    assert r.action == LoadAction.SHED and r.outcome == DecisionOutcome.OK
    # 只需削 ac(3)：11.5-3=8.5 仍 >= 8，再削 hvac(4)：4.5 < 8 → 档位 2
    assert r.level_after == 2
    assert [i.device_id for i in r.plan.shed] == ["ac", "hvac"]
    assert r.plan.net_after_kw == 11.5 - 7.0
    assert r.plan.net_after_kw < 8.0  # 真正落进可解除区间，而非刚好低于触发线
    assert d.status()["sheds"] == {"ac": 3.0, "hvac": 4.0}


def test_tie_break_by_device_id_deterministic():
    """同优先级按 device_id 排序，结果确定。"""
    d = LoadDispatcher(Settings(controllable_loads=("b:1:2.0", "a:1:2.0")))
    r = d.evaluate(snap(11.0))
    log_case("tie-break", {"net": 11.0}, r.model_dump())
    assert [i.device_id for i in r.plan.shed] == ["a", "b"]


def test_level_escalation_and_hysteresis_hold():
    """档位迁移确定：净负荷继续升高则升档；滞回带内保持不抖动。"""
    d = make_dispatcher()
    d.evaluate(snap(11.5))  # 档位 2（削 7）
    assert d.level == 2
    # 毛净负荷升到 16：有效净负荷 16-7=9 在滞回带 (8,10] 内 → 保持
    r = d.evaluate(snap(16.0))
    log_case("hysteresis-hold-level", {"net": 16.0}, r.model_dump())
    assert r.action == LoadAction.NONE and d.level == 2
    # 毛净负荷升到 18：有效 11 > 10 → 升档到 3（再削 fridge 2 → 有效 9... 仍>=8?）
    r = d.evaluate(snap(18.0))
    log_case("escalate-level-3", {"net": 18.0}, r.model_dump())
    assert r.action == LoadAction.SHED and r.level_after == 3
    assert r.plan.applied[0].device_id == "fridge"  # 只新增削减 fridge
    assert r.plan.net_after_kw == 18.0 - 9.0
    # 档位 3 下有效净负荷 9，处于滞回带：反复评估保持档位 3
    r = d.evaluate(snap(18.0))
    assert r.action == LoadAction.NONE and d.level == 3


def test_restore_reverse_order_and_no_flap_back():
    """恢复：一次性归还到档位 0，归还顺序按优先级降序（最重要先恢复）。"""
    d = make_dispatcher()
    d.evaluate(snap(20.0))  # 全部削减（容量不足也在档位 3）
    assert d.level == 3
    r = d.evaluate(snap(7.5))  # 毛净负荷 < 8 且 soc 达标 → 恢复
    log_case("graded-restore", {"net": 7.5}, r.model_dump())
    assert r.action == LoadAction.RESTORE and r.level_after == 0
    assert d.mode == SystemMode.NORMAL
    # 归还顺序：priority 降序（fridge 最先恢复）
    assert [i.device_id for i in r.plan.restored] == ["fridge", "hvac", "ac"]
    assert d.status()["sheds"] == {}
    # 恢复后净负荷 7.5 远低于触发线 10：不会刚恢复又弹回限载
    r = d.evaluate(snap(7.5))
    assert r.action == LoadAction.NONE and d.mode == SystemMode.NORMAL


def test_restore_blocked_in_hysteresis_band():
    """毛净负荷仍在滞回带内：保持削减，避免刚降下去又弹回来。"""
    d = make_dispatcher()
    d.evaluate(snap(11.5))
    assert d.level == 2
    r = d.evaluate(snap(9.0))  # 9 在 (8,10] 滞回带 → 保持
    log_case("restore-blocked-band", {"net": 9.0}, r.model_dump())
    assert r.action == LoadAction.NONE and d.level == 2
    assert d.status()["sheds"] == {"ac": 3.0, "hvac": 4.0}


def test_insufficient_capacity_sheds_all_and_is_distinguishable():
    """全部可控容量也不够：削减所有负载并给出可区分结果，不停在一半。"""
    d = make_dispatcher()  # 总容量 9kW
    r = d.evaluate(snap(25.0))
    log_case("insufficient-capacity", {"net": 25.0}, r.model_dump())
    assert r.outcome == DecisionOutcome.INSUFFICIENT_CAPACITY
    assert r.action == LoadAction.SHED and r.level_after == 3
    assert d.status()["total_shed_kw"] == 9.0  # 全部削减，不是削一半
    assert r.plan.net_after_kw == 16.0  # 仍高于恢复线，结果中可区分
    # 同一快照再评估：不叠加、不重复削减
    r2 = d.evaluate(snap(25.0))
    assert r2.action == LoadAction.NONE and d.status()["total_shed_kw"] == 9.0


def test_repeated_evaluation_is_idempotent():
    """同一份状态反复评估：结果完全一致，削减量不叠加。"""
    d = make_dispatcher()
    r1 = d.evaluate(snap(11.5))
    r2 = d.evaluate(snap(11.5))
    r3 = d.evaluate(snap(11.5))
    log_case("idempotent-evaluate", {"net": 11.5, "times": 3},
             [r.action for r in (r1, r2, r3)])
    assert r1.action == LoadAction.SHED
    assert r2.action == LoadAction.NONE and r3.action == LoadAction.NONE
    assert r2.level_after == r3.level_after == 2
    assert d.status()["sheds"] == {"ac": 3.0, "hvac": 4.0}  # 绝对量，未叠加
    assert r2.plan.shed == r3.plan.shed  # 计划内容一致


def test_invalid_snapshot_gives_stable_outcome_and_keeps_state():
    """输入非法（NaN/越界 soc）：可区分结果，状态不被带坏。"""
    d = make_dispatcher()
    d.evaluate(snap(11.5))
    before = d.status()
    bad = SystemSnapshot(load_kw=float("nan"), generation_kw=0.0,
                         storage_soc=0.5, net_load_kw=float("nan"))
    r = d.evaluate(bad)
    log_case("invalid-input-nan", "net_load=NaN", r.model_dump())
    assert r.outcome == DecisionOutcome.INVALID_INPUT
    assert r.action == LoadAction.NONE
    assert d.status() == before  # 已一致的状态不受影响
    bad_soc = snap(5.0, soc=1.5)
    r2 = d.evaluate(bad_soc)
    assert r2.outcome == DecisionOutcome.INVALID_INPUT
    assert d.status() == before


def test_missing_load_info_gives_distinguishable_outcome(tmp_path):
    """可控负载缺少必需信息（如注册表损坏缺字段）：可区分结果且不动作。"""
    store = str(tmp_path / "events.jsonl")
    d = LoadDispatcher(Settings(store_path=store,
                                controllable_loads=("ac:1:3.0",)))
    # 模拟持久化注册表被写入缺字段的条目
    d._loads["broken"] = {"priority": 2}  # 缺 sheddable_kw
    r = d.evaluate(snap(11.5))
    log_case("invalid-load-config", {"broken": {"priority": 2}}, r.model_dump())
    assert r.outcome == DecisionOutcome.INVALID_LOAD_CONFIG
    assert r.action == LoadAction.NONE
    assert d.mode == SystemMode.NORMAL and d.level == 0  # 不削一半


def test_concurrent_evaluate_and_commands_single_plan():
    """并发评估与手动命令：串行裁决，无重复削减/重复归还，最终状态确定。"""
    d = make_dispatcher()
    results, errors = [], []

    def run_eval():
        try:
            results.append(d.evaluate(snap(11.5)).action)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    def run_cmd(cmd):
        try:
            results.append(d.command(cmd, snap(11.5)).action)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = ([threading.Thread(target=run_eval) for _ in range(10)]
               + [threading.Thread(target=run_cmd, args=("shed",))
                  for _ in range(5)])
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log_case("concurrent-graded", "evaluate x10 + shed x5",
             {"actions": [a.value for a in results],
              "status": d.status()})
    assert not errors and len(results) == 15
    # 真实的限载迁移恰好一次，其余为确定性 no-op
    assert results.count(LoadAction.SHED) == 1
    assert d.status()["sheds"] == {"ac": 3.0, "hvac": 4.0}  # 无重复削减
    # 历史与最终状态一致
    assert d.decisions()[-1].level_after == d.level


def test_restart_recovers_level_sheds_and_history(tmp_path):
    """重启后：档位、被削减负载、历史口径与重启前一致。"""
    settings = Settings(store_path=str(tmp_path / "events.jsonl"),
                        controllable_loads=LOADS)
    d1 = LoadDispatcher(settings)
    d1.evaluate(snap(18.0))  # 档位 3
    before = d1.status()
    n_decisions = len(d1.decisions())

    d2 = LoadDispatcher(settings)  # 模拟重启
    log_case("restart-graded", settings.store_path,
             {"before": before, "after": d2.status()})
    assert d2.status() == before  # 模式/档位/削减明细一致
    assert len(d2.decisions()) == n_decisions  # 历史不重复
    # 重启后继续评估：口径与重启前一致（滞回保持，不重新削减）
    r = d2.evaluate(snap(18.0))
    assert r.action == LoadAction.NONE and d2.level == 3
    r = d2.evaluate(snap(7.0))
    assert r.action == LoadAction.RESTORE and d2.level == 0


def test_manual_command_graded_and_load_registration(tmp_path):
    """手动命令走同一套档位逻辑；运行时注册负载并持久化。"""
    settings = Settings(store_path=str(tmp_path / "events.jsonl"))
    d = LoadDispatcher(settings)
    d.register_load("ac", 1, 3.0)
    d.register_load("hvac", 2, 4.0)
    r = d.command("shed", snap(11.5))
    log_case("manual-shed-graded", {"net": 11.5}, r.model_dump())
    assert r.action == LoadAction.SHED and r.level_after == 2
    # 注册表持久化：重启后负载配置仍在
    d2 = LoadDispatcher(settings)
    assert [l["device_id"] for l in d2.list_loads()] == ["ac", "hvac"]
    assert d2.status()["sheds"] == {"ac": 3.0, "hvac": 4.0}
    r = d2.command("restore", snap(11.5))
    assert r.action == LoadAction.RESTORE and d2.level == 0
