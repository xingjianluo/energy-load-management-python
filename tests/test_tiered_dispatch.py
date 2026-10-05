"""分级削减：选择顺序、档位迁移与滞回、容量不足、缺配置、非法输入、
重复评估不叠加、并发确定性、重启恢复、旧记录兼容。"""
from __future__ import annotations

import json
import threading

from app.config import Settings
from app.dispatch import LoadDispatcher
from app.models import LoadAction, PlanStatus, SystemMode, SystemSnapshot

from tests.conftest import log_case

# 负载：hvac(优先级1, 3kW) ev(优先级2, 2kW) pool(优先级3, 1kW)，共 6kW
# 阈值：shed=10, restore=8, margin=0.5 → 目标区间 <7.5，归还上限 9.5
LOADS = ("hvac:1:3", "ev:2:2", "pool:3:1")
SETTINGS = Settings(
    shed_threshold_kw=10.0, restore_threshold_kw=8.0,
    shed_target_margin_kw=0.5, storage_low_soc=0.2,
    controllable_loads=LOADS,
)


def snap(net: float, soc: float = 0.5) -> SystemSnapshot:
    return SystemSnapshot(
        load_kw=net, generation_kw=0.0, storage_soc=soc, net_load_kw=net
    )


def make_dispatcher(settings: Settings | None = None) -> LoadDispatcher:
    return LoadDispatcher(settings or SETTINGS)


def test_shed_selection_order_and_plan():
    """触发限载：按优先级升序选择，产出明确计划且净负荷落入目标区间。"""
    d = make_dispatcher()
    r = d.evaluate(snap(10.4))
    log_case("tiered-shed-level1", {"net": 10.4}, r.model_dump())
    assert r.action == LoadAction.SHED and r.level_after == 1
    plan = r.plan
    assert plan is not None and plan.status == PlanStatus.OK
    # 只削优先级最低的 hvac（3kW 已足够覆盖 needed=2.9kW）
    assert [(s.device_id, s.shed_kw) for s in plan.steps] == [("hvac", 3.0)]
    # 净负荷真正落入可解除区间（< restore - margin = 7.5），而非刚好低于触发线
    assert plan.projected_net_load_kw == 10.4 - 3.0
    assert plan.projected_net_load_kw < plan.target_net_load_kw == 7.5
    assert d.mode == SystemMode.SHEDDING and d.level == 1

    r = d.evaluate(snap(13.0))
    log_case("tiered-shed-level3", {"net": 13.0}, r.model_dump())
    assert r.level_after == 3  # needed=5.5 → 全部三级
    assert [s.device_id for s in r.plan.steps] == ["hvac", "ev", "pool"]
    assert r.plan.total_shed_kw == 6.0
    assert r.plan.projected_net_load_kw == 7.0 < 7.5


def test_level_transitions_and_hysteresis():
    """档位迁移确定、滞回带内保持、边界严格比较。"""
    d = make_dispatcher()
    d.evaluate(snap(11.0))  # needed=3.5 → level 2
    assert d.level == 2
    # 滞回带内（8 <= net <= 10）：保持档位不抖动
    for net in (9.5, 8.0, 10.0):
        r = d.evaluate(snap(net))
        log_case("tiered-hysteresis-hold", {"net": net}, r.model_dump())
        assert r.action == LoadAction.NONE and r.level_after == 2
    # 超过触发线：按新需求重定档（needed=9.5 → 顶档）
    r = d.evaluate(snap(17.0))
    assert r.action == LoadAction.SHED and r.level_after == 3
    # 回落到滞回带：保持
    r = d.evaluate(snap(9.0))
    assert r.action == LoadAction.NONE and r.level_after == 3


def test_graded_restore_reverse_order():
    """恢复按相反顺序逐级归还：归还后预计净负荷不超过 shed - margin。"""
    d = make_dispatcher()
    d.evaluate(snap(13.0))  # level 3
    # net=5：全归还预计 5+6=11 > 9.5，归还 ev+pool 后预计 5+3=8 <= 9.5 → 降到 1 档
    r = d.evaluate(snap(5.0))
    log_case("tiered-graded-restore", {"net": 5.0}, r.model_dump())
    assert r.action == LoadAction.RESTORE and r.level_after == 1
    assert [s.device_id for s in r.plan.steps] == ["hvac"]  # 后削的 ev/pool 先归还
    assert d.mode == SystemMode.SHEDDING  # 仍在限载，未一刀切恢复
    # net=1：全归还预计 7 <= 9.5 → 回到 0 档
    r = d.evaluate(snap(1.0))
    log_case("tiered-full-restore", {"net": 1.0}, r.model_dump())
    assert r.level_after == 0 and d.mode == SystemMode.NORMAL


def test_low_soc_blocks_restore_in_tiered():
    d = make_dispatcher()
    d.evaluate(snap(11.0))  # level 2
    r = d.evaluate(snap(5.0, soc=0.1))  # 低电量：不得降档
    log_case("tiered-low-soc-hold", {"net": 5.0, "soc": 0.1}, r.model_dump())
    assert r.level_after == 2 and d.mode == SystemMode.SHEDDING
    r = d.evaluate(snap(5.0, soc=0.5))  # 电量恢复：允许逐级归还
    assert r.level_after == 1


def test_insufficient_capacity():
    """全部可削减容量也不足以压进目标区间：削到顶档并给出可区分结果。"""
    d = make_dispatcher()
    r = d.evaluate(snap(20.0))  # needed=12.5 > 总容量 6
    log_case("tiered-insufficient", {"net": 20.0}, r.model_dump())
    assert r.plan.status == PlanStatus.INSUFFICIENT_CAPACITY
    assert r.level_after == 3 and r.plan.total_shed_kw == 6.0  # 不削一半停手
    assert r.plan.projected_net_load_kw == 14.0 > 7.5  # 明确仍未达标
    assert d.mode == SystemMode.SHEDDING


def test_missing_load_config():
    """可控负载缺必需信息：状态不被带坏，结果可区分。"""
    d = make_dispatcher(Settings(controllable_loads=("hvac:1:3", "bad:1", ":2:5")))
    r = d.evaluate(snap(20.0))
    log_case("tiered-missing-config", {"net": 20.0}, r.model_dump())
    assert r.plan.status == PlanStatus.MISSING_LOAD_CONFIG
    assert r.action == LoadAction.NONE
    assert d.level == 0 and d.mode == SystemMode.NORMAL  # 不削一半停手
    r2 = d.evaluate(snap(20.0))  # 反复评估结果一致
    assert r2.plan.status == PlanStatus.MISSING_LOAD_CONFIG and d.level == 0


def test_invalid_input_snapshot():
    """输入状态不合法：不改变已一致状态，结果可区分。"""
    d = make_dispatcher()
    d.evaluate(snap(11.0))  # level 2
    bad = SystemSnapshot(load_kw=5.0, generation_kw=0.0,
                         storage_soc=0.5, net_load_kw=float("nan"))
    r = d.evaluate(bad)
    log_case("tiered-invalid-nan", {"net": "nan"}, r.model_dump())
    assert r.plan.status == PlanStatus.INVALID_INPUT
    assert d.level == 2 and d.mode == SystemMode.SHEDDING  # 状态不被带坏
    inconsistent = SystemSnapshot(load_kw=5.0, generation_kw=0.0,
                                  storage_soc=0.5, net_load_kw=9.0)
    r = d.evaluate(inconsistent)
    log_case("tiered-invalid-inconsistent", inconsistent.model_dump(), r.model_dump())
    assert r.plan.status == PlanStatus.INVALID_INPUT and d.level == 2


def test_repeated_evaluation_not_stacking():
    """同一份状态反复评估：计划完全一致，削减量不叠加。"""
    d = make_dispatcher()
    r1 = d.evaluate(snap(11.0))
    r2 = d.evaluate(snap(11.0))
    r3 = d.evaluate(snap(11.0))
    log_case("tiered-idempotent", {"net": 11.0, "evals": 3},
             {"levels": [r1.level_after, r2.level_after, r3.level_after],
              "total_shed": [r.plan.total_shed_kw for r in (r1, r2, r3)]})
    assert (r1.level_after, r2.level_after, r3.level_after) == (2, 2, 2)
    assert r2.action == LoadAction.NONE and r3.action == LoadAction.NONE
    # 削减量恒为档位对应值，不随评估次数叠加
    assert all(r.plan.total_shed_kw == 5.0 for r in (r1, r2, r3))
    assert [s.model_dump() for s in r1.plan.steps] == \
           [s.model_dump() for s in r3.plan.steps]


def test_concurrent_evaluate_and_commands():
    """并发评估与命令：决策链首尾相接，最终状态确定且自洽。"""
    d = make_dispatcher()
    errors: list[Exception] = []

    def run_eval(net: float) -> None:
        try:
            d.evaluate(snap(net))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    def run_cmd(cmd: str) -> None:
        try:
            d.command(cmd, snap(5.0))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = []
    for i in range(10):
        threads.append(threading.Thread(target=run_eval, args=(11.0 + i,)))
        threads.append(threading.Thread(target=run_cmd, args=("shed" if i % 2 else "restore",)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    recs = d.decisions()
    log_case("tiered-concurrent", "10 evals + 10 commands",
             {"decisions": len(recs), "final_level": d.level, "mode": d.mode})
    assert not errors and len(recs) == 20
    # 决策链完整：每条记录的 before 与上一条的 after 一致（无并发撕裂）
    for prev, cur in zip(recs, recs[1:]):
        assert cur.level_before == prev.level_after
        assert cur.mode_before == prev.mode_after
    # 最终状态自洽：档位与模式一致，计划与档位一致
    assert d.mode == (SystemMode.SHEDDING if d.level > 0 else SystemMode.NORMAL)
    plan = d.current_plan()
    assert plan.level == d.level and len(plan.steps) == d.level
    # 无重复削减：档位对应的前缀负载集合唯一
    assert len({s.device_id for s in plan.steps}) == len(plan.steps)


def test_level_persists_across_restart(tmp_path):
    settings = Settings(
        shed_threshold_kw=10.0, restore_threshold_kw=8.0,
        shed_target_margin_kw=0.5, controllable_loads=LOADS,
        store_path=str(tmp_path / "events.jsonl"),
    )
    d1 = make_dispatcher(settings)
    d1.evaluate(snap(11.0))  # level 2
    assert d1.level == 2 and d1.mode == SystemMode.SHEDDING
    d2 = make_dispatcher(settings)  # 模拟重启
    log_case("tiered-restart", settings.store_path,
             {"level_before": 2, "level_after_restart": d2.level})
    assert d2.level == 2 and d2.mode == SystemMode.SHEDDING
    assert len(d2.decisions()) == 1  # 历史不重复
    # 重启后计划口径与重启前一致
    assert [s.device_id for s in d2.current_plan().steps] == ["hvac", "ev"]
    # 重启后继续评估不叠加
    r = d2.evaluate(snap(11.0))
    assert r.level_after == 2 and r.action == LoadAction.NONE


def test_legacy_decision_records_compatible(tmp_path):
    """旧格式决策记录（无 level/plan 字段）可直接重放。"""
    path = tmp_path / "events.jsonl"
    legacy = {
        "decision_id": "old-1", "decided_at": "2026-09-24T12:00:00+00:00",
        "mode_before": "normal", "mode_after": "shedding", "action": "shed",
        "reasons": ["legacy"],
        "snapshot": {"load_kw": 11.0, "generation_kw": 0.0,
                     "storage_soc": 0.5, "net_load_kw": 11.0, "devices": {}},
        "thresholds": {"shed_threshold_kw": 10.0},
    }
    with open(str(path) + ".decisions.jsonl", "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"event_id": "old-1", "record": legacy}) + "\n")
    d = LoadDispatcher(Settings(store_path=str(path)))
    log_case("tiered-legacy-record", legacy, {"mode": d.mode, "level": d.level})
    assert d.mode == SystemMode.SHEDDING and d.level == 0
    assert d.decisions()[0].plan is None  # 旧记录无计划，兼容读取
