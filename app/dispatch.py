"""负载调度：整体限载（旧行为）与按优先级的分级削减（tiered）。

未配置可控负载（controllable_loads 为空）时保持旧行为：
NORMAL/SHEDDING 两模式 + 滞回，决策语义与历史记录结构完全不变。

配置可控负载后进入分级模式，档位（level）语义：
- level 0：不削减；level L：按 (priority, device_id) 升序削减前 L 个负载。
- 档位是快照的确定函数：同一状态反复评估得到同一份计划，削减量不叠加。
- 触发（net > shed_threshold 或 soc < low_soc）：升到能把净负荷压进
  目标区间（< restore_threshold - margin）的最低档；容量不足时升到顶档
  并给出 insufficient_capacity，不削一半停手。
- 恢复（net < restore_threshold 且 soc 达标）：按相反顺序逐级归还，
  归还后预计净负荷不超过 shed_threshold - margin，避免刚降又弹回。
- 滞回带内保持当前档位，不抖动。
所有状态变更在同一把锁内完成：并发评估/命令被串行化，
同一时刻至多产生一次真实的档位迁移，结果确定。
"""
from __future__ import annotations

import logging
import math
import threading
import uuid
from datetime import UTC, datetime
from typing import Any, Callable

from app.config import Settings
from app.models import (
    ControllableLoad,
    DecisionRecord,
    LoadAction,
    PlanStatus,
    ShedPlan,
    ShedStep,
    SystemMode,
    SystemSnapshot,
    parse_controllable_loads,
)
from app.storage import EventStore

logger = logging.getLogger("elm.dispatch")


class LoadDispatcher:
    """线程安全的调度器。"""

    def __init__(
        self,
        settings: Settings,
        now: Callable[[], datetime] | None = None,
        decision_store_path: str | None = None,
    ) -> None:
        self._settings = settings
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._mode = SystemMode.NORMAL
        self._level = 0
        self._decisions: list[DecisionRecord] = []
        loads, invalid = parse_controllable_loads(settings.controllable_loads)
        self._loads: list[ControllableLoad] = loads  # 削减顺序（升序优先级）
        self._invalid_load_specs: list[str] = invalid
        self._cum: list[float] = [0.0]  # 前缀和：cum[L] = 前 L 个负载的可削减容量和
        for load in loads:
            self._cum.append(self._cum[-1] + load.sheddable_kw)
        path = decision_store_path or (
            settings.store_path + ".decisions.jsonl" if settings.store_path else None
        )
        self._store = EventStore(path) if path else EventStore(None)
        # 重启恢复：重放决策历史，恢复最后模式与档位，不重复记录
        for ev in self._store.all_events():
            rec = DecisionRecord.model_validate(ev["record"])
            self._decisions.append(rec)
            self._mode = rec.mode_after
            self._level = rec.level_after

    @property
    def mode(self) -> SystemMode:
        return self._mode

    @property
    def level(self) -> int:
        return self._level

    @property
    def tiered(self) -> bool:
        """是否启用分级削减（配置了至少一个可控负载）。"""
        return bool(self._loads)

    # ---------- 分级削减：纯判定 ----------

    def _validate_snapshot(self, snapshot: SystemSnapshot) -> list[str]:
        """输入状态合法性校验；返回问题列表（空表示合法）。"""
        problems: list[str] = []
        for name in ("net_load_kw", "load_kw", "generation_kw", "storage_soc"):
            v = getattr(snapshot, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                problems.append(f"{name} is not a finite number: {v!r}")
        if problems:
            return problems
        if not 0.0 <= snapshot.storage_soc <= 1.0:
            problems.append(f"storage_soc out of range [0,1]: {snapshot.storage_soc}")
        expected = snapshot.load_kw - snapshot.generation_kw
        if abs(snapshot.net_load_kw - expected) > 1e-6:
            problems.append(
                f"net_load_kw {snapshot.net_load_kw} inconsistent with "
                f"load_kw - generation_kw = {expected}"
            )
        return problems

    def _target_level(self, snapshot: SystemSnapshot) -> tuple[int, PlanStatus, list[str]]:
        """由快照确定目标档位（纯函数 + 滞回保持），保证重复评估不叠加。"""
        s = self._settings
        net, soc = snapshot.net_load_kw, snapshot.storage_soc
        n = len(self._loads)
        target_zone = s.restore_threshold_kw - s.shed_target_margin_kw
        reasons: list[str] = []
        if net > s.shed_threshold_kw or soc < s.storage_low_soc:
            # 触发：升到能把净负荷压进目标区间的最低档
            needed = net - target_zone
            level = 0
            while level < n and self._cum[level] < needed:
                level += 1
            if soc < s.storage_low_soc:
                # 低电量期间只允许升档，不允许借评估降档（防抖动）
                level = max(level, self._level)
            status = PlanStatus.OK
            if self._cum[level] < needed:
                status = PlanStatus.INSUFFICIENT_CAPACITY
            reasons.append(
                f"trigger: net_load {net:.3f}kW > shed {s.shed_threshold_kw}kW "
                f"or soc {soc:.3f} < {s.storage_low_soc}; need shed "
                f"{max(needed, 0.0):.3f}kW to reach target zone "
                f"< {target_zone:.3f}kW -> level {level}"
            )
            if status == PlanStatus.INSUFFICIENT_CAPACITY:
                reasons.append(
                    f"total sheddable {self._cum[n]:.3f}kW < required "
                    f"{needed:.3f}kW: insufficient capacity, shed all"
                )
            return level, status, reasons
        if net < s.restore_threshold_kw and soc >= s.storage_low_soc:
            # 恢复：按相反顺序逐级归还，归还后预计净负荷不超过
            # shed_threshold - margin（防刚归还又触发）
            limit = s.shed_threshold_kw - s.shed_target_margin_kw
            min_level = n
            for cand in range(0, n + 1):
                projected = net + self._cum[n] - self._cum[cand]
                if projected <= limit:
                    min_level = cand
                    break
            level = min(self._level, min_level)  # 恢复分支绝不升档
            restored = self._cum[n] - self._cum[level]
            reasons.append(
                f"restore: net_load {net:.3f}kW < restore {s.restore_threshold_kw}kW; "
                f"return loads in reverse priority order while projected net "
                f"<= {limit:.3f}kW -> level {level} (return {restored:.3f}kW)"
            )
            return level, PlanStatus.OK, reasons
        # 滞回带：保持当前档位
        reasons.append(
            f"hysteresis hold: net_load {net:.3f}kW in "
            f"[{s.restore_threshold_kw}, {s.shed_threshold_kw}]kW, "
            f"soc {soc:.3f}: keep level {self._level}"
        )
        return self._level, PlanStatus.OK, reasons

    def _plan_for(
        self, level: int, snapshot: SystemSnapshot,
        status: PlanStatus, reasons: list[str],
    ) -> ShedPlan:
        """由档位生成确定计划：削哪些负载、各减多少、削减后净负荷。"""
        s = self._settings
        steps = [
            ShedStep(device_id=l.device_id, priority=l.priority, shed_kw=l.sheddable_kw)
            for l in self._loads[:level]
        ]
        total = self._cum[level]
        return ShedPlan(
            level=level,
            steps=steps,
            total_shed_kw=total,
            projected_net_load_kw=snapshot.net_load_kw - total,
            target_net_load_kw=s.restore_threshold_kw - s.shed_target_margin_kw,
            status=status,
            reasons=reasons,
        )

    # ---------- 旧模式：整体限载判定 ----------

    def _decide_legacy(self, snapshot: SystemSnapshot) -> tuple[LoadAction, list[str]]:
        """纯判定逻辑：给定当前模式与快照，返回动作与判定依据。"""
        s = self._settings
        net = snapshot.net_load_kw
        soc = snapshot.storage_soc
        reasons: list[str] = []
        if self._mode == SystemMode.NORMAL:
            if net > s.shed_threshold_kw:
                reasons.append(
                    f"net_load {net:.3f}kW > shed_threshold {s.shed_threshold_kw}kW"
                )
            if soc < s.storage_low_soc:
                reasons.append(
                    f"storage_soc {soc:.3f} < low_soc {s.storage_low_soc}"
                )
            if reasons:
                return LoadAction.SHED, reasons
            return LoadAction.NONE, [
                f"net_load {net:.3f}kW <= shed_threshold {s.shed_threshold_kw}kW, "
                f"soc {soc:.3f} >= low_soc {s.storage_low_soc}: hold normal"
            ]
        # SHEDDING：解除需同时满足两个条件，否则保持（滞回）
        restore_ok = net < s.restore_threshold_kw and soc >= s.storage_low_soc
        if restore_ok:
            reasons.append(
                f"net_load {net:.3f}kW < restore_threshold {s.restore_threshold_kw}kW "
                f"and soc {soc:.3f} >= low_soc {s.storage_low_soc}"
            )
            return LoadAction.RESTORE, reasons
        return LoadAction.NONE, [
            f"hysteresis hold: net_load {net:.3f}kW, soc {soc:.3f}; "
            f"restore requires net_load < {s.restore_threshold_kw}kW "
            f"and soc >= {s.storage_low_soc}"
        ]

    # ---------- 决策落盘 ----------

    def _record(
        self,
        snapshot: SystemSnapshot,
        action: LoadAction,
        reasons: list[str],
        *,
        mode_before: SystemMode,
        level_before: int,
        plan: ShedPlan | None = None,
    ) -> DecisionRecord:
        rec = DecisionRecord(
            decision_id=str(uuid.uuid4()),
            decided_at=self._now(),
            mode_before=mode_before,
            mode_after=self._mode,
            action=action,
            reasons=reasons,
            snapshot=snapshot,
            thresholds={
                "shed_threshold_kw": self._settings.shed_threshold_kw,
                "restore_threshold_kw": self._settings.restore_threshold_kw,
                "storage_low_soc": self._settings.storage_low_soc,
                "shed_target_margin_kw": self._settings.shed_target_margin_kw,
            },
            level_before=level_before,
            level_after=self._level,
            plan=plan,
        )
        self._decisions.append(rec)
        self._store.append({"event_id": rec.decision_id,
                            "record": rec.model_dump(mode="json")})
        logger.info(
            "decision %s action=%s mode %s->%s level %s->%s reasons=%s",
            rec.decision_id, action, mode_before, self._mode,
            level_before, self._level, reasons,
        )
        return rec

    def _apply(self, target_level: int) -> LoadAction:
        """把档位/模式迁移到目标值，返回对应的调度动作。"""
        if target_level > self._level:
            action = LoadAction.SHED
        elif target_level < self._level:
            action = LoadAction.RESTORE
        else:
            action = LoadAction.NONE
        self._level = target_level
        self._mode = SystemMode.SHEDDING if target_level > 0 else SystemMode.NORMAL
        return action

    # ---------- 对外入口 ----------

    def evaluate(self, snapshot: SystemSnapshot) -> DecisionRecord:
        """根据快照评估并应用决策（自动调度入口）。"""
        with self._lock:
            mode_before, level_before = self._mode, self._level
            if not self.tiered:
                action, reasons = self._decide_legacy(snapshot)
                if action == LoadAction.SHED:
                    self._mode = SystemMode.SHEDDING
                elif action == LoadAction.RESTORE:
                    self._mode = SystemMode.NORMAL
                return self._record(snapshot, action, reasons,
                                    mode_before=mode_before, level_before=0)
            problems = self._validate_snapshot(snapshot)
            if problems:
                # 输入不合法：不改变任何已一致状态，给出可区分结果
                plan = ShedPlan(level=self._level, status=PlanStatus.INVALID_INPUT,
                                reasons=problems)
                logger.warning("evaluate rejected: %s", problems)
                return self._record(snapshot, LoadAction.NONE, problems,
                                    mode_before=mode_before,
                                    level_before=level_before, plan=plan)
            if self._invalid_load_specs:
                # 有可控负载缺必需信息：不削一半停手，保持现状并明确上报
                reasons = [
                    f"invalid controllable load specs (need id:priority:sheddable_kw): "
                    f"{self._invalid_load_specs}"
                ]
                plan = ShedPlan(level=self._level,
                                status=PlanStatus.MISSING_LOAD_CONFIG,
                                reasons=reasons)
                logger.warning("evaluate blocked: %s", reasons)
                return self._record(snapshot, LoadAction.NONE, reasons,
                                    mode_before=mode_before,
                                    level_before=level_before, plan=plan)
            target, status, reasons = self._target_level(snapshot)
            plan = self._plan_for(target, snapshot, status, reasons)
            action = self._apply(target)
            return self._record(snapshot, action, reasons,
                                mode_before=mode_before,
                                level_before=level_before, plan=plan)

    def command(self, cmd: str, snapshot: SystemSnapshot) -> DecisionRecord:
        """手动命令（shed/restore）。

        并发冲突命令在锁内串行：后到命令若与当前状态一致则为确定性 no-op，
        同一时刻至多产生一次真实迁移。分级模式下 shed=升至顶档、
        restore=归还到 0 档；未配置可控负载时保持旧的整体限载语义。
        """
        with self._lock:
            mode_before, level_before = self._mode, self._level
            if cmd not in ("shed", "restore"):
                raise ValueError(f"unknown command: {cmd}")
            if not self.tiered:
                target = {
                    "shed": SystemMode.SHEDDING,
                    "restore": SystemMode.NORMAL,
                }[cmd]
                if target == self._mode:
                    return self._record(
                        snapshot, LoadAction.NONE,
                        [f"command {cmd}: already in mode {self._mode}, no-op"],
                        mode_before=mode_before, level_before=0,
                    )
                self._mode = target
                action = (LoadAction.SHED if target == SystemMode.SHEDDING
                          else LoadAction.RESTORE)
                return self._record(snapshot, action,
                                    [f"manual command: {cmd}"],
                                    mode_before=mode_before, level_before=0)
            target_level = len(self._loads) if cmd == "shed" else 0
            if target_level == self._level:
                return self._record(
                    snapshot, LoadAction.NONE,
                    [f"command {cmd}: already at level {self._level}, no-op"],
                    mode_before=mode_before, level_before=level_before,
                    plan=self._plan_for(self._level, snapshot, PlanStatus.OK,
                                        ["no-op"]),
                )
            reasons = [f"manual command: {cmd} -> level {target_level}"]
            plan = self._plan_for(target_level, snapshot, PlanStatus.OK, reasons)
            action = self._apply(target_level)
            return self._record(snapshot, action, reasons,
                                mode_before=mode_before,
                                level_before=level_before, plan=plan)

    def current_plan(self) -> ShedPlan:
        """当前生效档位的计划视图（只读，不产生决策记录）。"""
        with self._lock:
            steps = [
                ShedStep(device_id=l.device_id, priority=l.priority,
                         shed_kw=l.sheddable_kw)
                for l in self._loads[:self._level]
            ]
            return ShedPlan(
                level=self._level,
                steps=steps,
                total_shed_kw=self._cum[self._level],
                status=(PlanStatus.MISSING_LOAD_CONFIG
                        if self._invalid_load_specs else PlanStatus.OK),
                reasons=["current effective plan"],
            )

    def decisions(self) -> list[DecisionRecord]:
        """全部决策历史（可追溯）。"""
        return list(self._decisions)
