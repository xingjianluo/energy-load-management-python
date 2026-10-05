"""负载调度：按优先级的分级削减，带滞回的状态机，决策可追溯，并发冲突只有一个确定结果。

档位模型：
- 可控负载按 (priority 升序, device_id) 排序，priority 数值越大越重要。
- 档位 L 表示排序后的前 L 个负载处于被削减状态；档位 0 = 未削减。
- 削减目标：净负荷真正落进可解除区间（net_after < restore_threshold），
  而不是刚好低于触发线；容量不足时削减全部并给出 insufficient_capacity。
- 恢复：仅当毛净负荷 < restore_threshold 且 soc 达标时一次性归还到档位 0，
  归还顺序按优先级降序（最重要先恢复）；恢复后净负荷必然低于触发线，不会回弹。

滞回规则（net_load = load_kw - generation_kw，快照为未削减口径）：
- NORMAL 下 eff_net > shed_threshold 或 soc < storage_low_soc → 触发限载(SHED)
- SHEDDING 下 eff_net > shed_threshold → 升档；net < restore_threshold 且
  soc 达标 → 归还至档位 0(RESTORE)；其余保持（滞回带不抖动）
- eff_net = net_load - 当前已削减量；目标档位由快照确定性算出，
  同一快照反复评估结果完全一致，不会叠加削减量。

所有状态变更在同一把锁内完成：并发评估/命令被串行化，最终只有一份确定的计划。
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import uuid
from datetime import UTC, datetime
from typing import Any, Callable

from app.config import Settings
from app.models import (
    ControllableLoad,
    DecisionOutcome,
    DecisionRecord,
    LoadAction,
    ShedItem,
    ShedPlan,
    SystemMode,
    SystemSnapshot,
)
from app.storage import EventStore

logger = logging.getLogger("elm.dispatch")


class LoadDispatcher:
    """线程安全的分级调度器。"""

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
        self._sheds: dict[str, float] = {}  # 当前生效：device_id -> 已削减 kW
        self._decisions: list[DecisionRecord] = []
        self._loads: dict[str, dict[str, Any]] = {}  # device_id -> 原始配置
        self._loads_path = (
            settings.store_path + ".loads.json" if settings.store_path else None
        )
        self._load_registry()
        # 环境变量预注册（注册表文件优先，缺省补齐）；条目非法时启动即失败
        for entry in settings.controllable_loads:
            device_id, prio, kw = self._parse_entry(entry)
            self._loads.setdefault(
                device_id, {"priority": prio, "sheddable_kw": kw}
            )
        path = decision_store_path or (
            settings.store_path + ".decisions.jsonl" if settings.store_path else None
        )
        self._store = EventStore(path) if path else EventStore(None)
        # 重启恢复：重放决策历史，恢复模式/档位/已削减集合，不重复记录
        for ev in self._store.all_events():
            rec = DecisionRecord.model_validate(ev["record"])
            self._decisions.append(rec)
            self._mode = rec.mode_after
            self._level = rec.level_after
            self._sheds = (
                {i.device_id: i.shed_kw for i in rec.plan.shed} if rec.plan else {}
            )

    # ---------- 可控负载配置 ----------

    @staticmethod
    def _parse_entry(entry: str) -> tuple[str, int, float]:
        """解析 'id:priority:sheddable_kw'；非法条目抛 ValueError（启动失败）。"""
        parts = entry.split(":")
        if len(parts) != 3 or not parts[0]:
            raise ValueError(
                f"invalid controllable load entry: {entry!r}, "
                "expected id:priority:sheddable_kw"
            )
        return parts[0], int(parts[1]), float(parts[2])

    def _load_registry(self) -> None:
        if self._loads_path and os.path.exists(self._loads_path):
            try:
                with open(self._loads_path, encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    self._loads.update(
                        {str(k): dict(v) for k, v in data.items()
                         if isinstance(v, dict)}
                    )
            except (OSError, json.JSONDecodeError):
                logger.warning("load registry corrupted, starting empty")

    def _save_registry(self) -> None:
        if not self._loads_path:
            return
        tmp = self._loads_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._loads, fh)
        os.replace(tmp, self._loads_path)

    @staticmethod
    def _validate_load(device_id: str, priority: Any, kw: Any) -> ControllableLoad:
        if not device_id:
            raise ValueError("device_id must be a non-empty string")
        if isinstance(priority, bool) or not isinstance(priority, int) or priority < 0:
            raise ValueError(f"load {device_id}: priority must be an integer >= 0")
        if (isinstance(kw, bool) or not isinstance(kw, (int, float))
                or not math.isfinite(kw) or kw <= 0):
            raise ValueError(f"load {device_id}: sheddable_kw must be a number > 0")
        return ControllableLoad(
            device_id=device_id, priority=priority, sheddable_kw=float(kw)
        )

    def register_load(self, device_id: str, priority: Any,
                      sheddable_kw: Any) -> dict[str, Any]:
        """注册/更新可控负载（同参数幂等）；非法配置抛 ValueError。"""
        load = self._validate_load(device_id, priority, sheddable_kw)
        with self._lock:
            self._loads[device_id] = {
                "priority": load.priority, "sheddable_kw": load.sheddable_kw,
            }
            self._save_registry()
            return {
                "device_id": device_id,
                "priority": load.priority,
                "sheddable_kw": load.sheddable_kw,
            }

    def list_loads(self) -> list[dict[str, Any]]:
        """全部可控负载配置（按削减顺序）。"""
        with self._lock:
            loads, _ = self._collect_loads()
            return [l.model_dump() for l in loads]

    def _collect_loads(self) -> tuple[list[ControllableLoad], list[str]]:
        """汇总配置并校验；返回 (按削减顺序排列的合法负载, 非法配置原因)。"""
        loads: list[ControllableLoad] = []
        errors: list[str] = []
        for device_id, cfg in sorted(self._loads.items()):
            try:
                loads.append(self._validate_load(
                    device_id, cfg.get("priority"), cfg.get("sheddable_kw")
                ))
            except ValueError as exc:
                errors.append(str(exc))
        loads.sort(key=lambda l: (l.priority, l.device_id))
        return loads, errors

    # ---------- 状态查询 ----------

    @property
    def mode(self) -> SystemMode:
        return self._mode

    @property
    def level(self) -> int:
        return self._level

    def status(self) -> dict[str, Any]:
        """当前模式、档位与生效中的削减明细。"""
        with self._lock:
            return {
                "mode": self._mode,
                "level": self._level,
                "sheds": dict(self._sheds),
                "total_shed_kw": sum(self._sheds.values()),
            }

    # ---------- 判定逻辑（锁内调用） ----------

    @staticmethod
    def _snapshot_errors(snapshot: SystemSnapshot) -> list[str]:
        errors = []
        for field in ("load_kw", "generation_kw", "net_load_kw", "storage_soc"):
            val = getattr(snapshot, field)
            if not isinstance(val, (int, float)) or not math.isfinite(val):
                errors.append(f"{field} is not a finite number: {val!r}")
        soc = snapshot.storage_soc
        if isinstance(soc, (int, float)) and math.isfinite(soc) and not 0.0 <= soc <= 1.0:
            errors.append(f"storage_soc out of range [0,1]: {soc}")
        return errors

    def _escalation_target(self, net: float,
                           loads: list[ControllableLoad]) -> tuple[int, bool]:
        """最小档位 L 使 net - 累计削减 < restore_threshold；返回 (档位, 是否达标)。"""
        s = self._settings
        cumulative = 0.0
        for idx, load in enumerate(loads):
            cumulative += load.sheddable_kw
            if net - cumulative < s.restore_threshold_kw:
                return idx + 1, True
        return len(loads), False

    def _decide(self, snapshot: SystemSnapshot,
                loads: list[ControllableLoad],
                ) -> tuple[LoadAction, int, DecisionOutcome, list[str]]:
        """纯判定：返回 (动作, 目标档位, 结果, 判定依据)。不产生副作用。"""
        s = self._settings
        net, soc = snapshot.net_load_kw, snapshot.storage_soc
        current_shed = sum(self._sheds.values())
        eff_net = net - current_shed
        reasons: list[str] = []

        trigger: list[str] = []
        if eff_net > s.shed_threshold_kw:
            trigger.append(
                f"effective net_load {eff_net:.3f}kW > shed_threshold "
                f"{s.shed_threshold_kw}kW"
            )
        if soc < s.storage_low_soc:
            trigger.append(f"storage_soc {soc:.3f} < low_soc {s.storage_low_soc}")

        if self._mode == SystemMode.NORMAL and not trigger:
            return LoadAction.NONE, 0, DecisionOutcome.OK, [
                f"effective net_load {eff_net:.3f}kW <= shed_threshold "
                f"{s.shed_threshold_kw}kW, soc {soc:.3f} >= low_soc "
                f"{s.storage_low_soc}: hold normal"
            ]

        restore_ok = net < s.restore_threshold_kw and soc >= s.storage_low_soc
        if self._mode == SystemMode.SHEDDING and restore_ok and not trigger:
            reasons.append(
                f"net_load {net:.3f}kW < restore_threshold "
                f"{s.restore_threshold_kw}kW and soc {soc:.3f} >= low_soc "
                f"{s.storage_low_soc}: restore all levels"
            )
            return LoadAction.RESTORE, 0, DecisionOutcome.OK, reasons

        if trigger and (self._mode == SystemMode.NORMAL
                        or eff_net > s.shed_threshold_kw):
            target, reached = self._escalation_target(net, loads)
            outcome = (DecisionOutcome.OK if reached
                       else DecisionOutcome.INSUFFICIENT_CAPACITY)
            reasons.extend(trigger)
            if reached:
                reasons.append(
                    f"target level {target}: net_after "
                    f"{net - sum(l.sheddable_kw for l in loads[:target]):.3f}kW "
                    f"< restore_threshold {s.restore_threshold_kw}kW"
                )
            else:
                reasons.append(
                    f"insufficient capacity: shedding all {len(loads)} loads "
                    f"({sum(l.sheddable_kw for l in loads):.3f}kW) still leaves "
                    f"net_after {net - sum(l.sheddable_kw for l in loads):.3f}kW "
                    f">= restore_threshold {s.restore_threshold_kw}kW"
                )
            if self._mode == SystemMode.SHEDDING and target == self._level:
                return LoadAction.NONE, target, outcome, [
                    f"already at target level {target}: no additional shed"
                ] + reasons
            return LoadAction.SHED, target, outcome, reasons

        # 滞回保持：解除需毛净负荷低于恢复线且 soc 达标
        return LoadAction.NONE, self._level, DecisionOutcome.OK, [
            f"hysteresis hold at level {self._level}: net_load {net:.3f}kW, "
            f"effective {eff_net:.3f}kW, soc {soc:.3f}; restore requires "
            f"net_load < {s.restore_threshold_kw}kW and soc >= {s.storage_low_soc}"
        ]

    # ---------- 决策记录 ----------

    def _build_plan(self, snapshot: SystemSnapshot,
                    loads: list[ControllableLoad],
                    target: int) -> tuple[ShedPlan, dict[str, float]]:
        """由目标档位构造削减计划与目标削减集合（绝对量，不叠加）。"""
        target_sheds = {l.device_id: l.sheddable_kw for l in loads[:target]}
        applied = [
            ShedItem(device_id=l.device_id, priority=l.priority,
                     shed_kw=l.sheddable_kw)
            for l in loads[:target]
            if self._sheds.get(l.device_id) != l.sheddable_kw
        ]
        restored = sorted(
            (ShedItem(device_id=l.device_id, priority=l.priority, shed_kw=kw)
             for l in loads
             if (kw := self._sheds.get(l.device_id)) is not None
             and l.device_id not in target_sheds),
            key=lambda i: (i.priority, i.device_id), reverse=True,
        )
        # 配置外的历史削减（重启后配置变更）也一并归还
        known = {l.device_id for l in loads}
        for dev, kw in self._sheds.items():
            if dev not in known and dev not in target_sheds:
                restored.append(ShedItem(device_id=dev, priority=-1, shed_kw=kw))
        plan = ShedPlan(
            target_level=target,
            net_before_kw=snapshot.net_load_kw,
            net_after_kw=snapshot.net_load_kw - sum(target_sheds.values()),
            shed=[ShedItem(device_id=l.device_id, priority=l.priority,
                           shed_kw=l.sheddable_kw) for l in loads[:target]],
            applied=applied,
            restored=restored,
        )
        return plan, target_sheds

    def _record(self, snapshot: SystemSnapshot, action: LoadAction,
                reasons: list[str], outcome: DecisionOutcome,
                target: int | None = None,
                loads: list[ControllableLoad] | None = None) -> DecisionRecord:
        before, level_before = self._mode, self._level
        plan: ShedPlan | None = None
        if target is not None and loads is not None:
            plan, self._sheds = self._build_plan(snapshot, loads, target)
            self._level = target
        if action == LoadAction.SHED:
            self._mode = SystemMode.SHEDDING
        elif action == LoadAction.RESTORE:
            self._mode = SystemMode.NORMAL
        rec = DecisionRecord(
            decision_id=str(uuid.uuid4()),
            decided_at=self._now(),
            mode_before=before,
            mode_after=self._mode,
            action=action,
            reasons=reasons,
            snapshot=snapshot,
            thresholds={
                "shed_threshold_kw": self._settings.shed_threshold_kw,
                "restore_threshold_kw": self._settings.restore_threshold_kw,
                "storage_low_soc": self._settings.storage_low_soc,
            },
            outcome=outcome,
            level_before=level_before,
            level_after=self._level,
            plan=plan,
        )
        self._decisions.append(rec)
        self._store.append({"event_id": rec.decision_id,
                            "record": rec.model_dump(mode="json")})
        logger.info(
            "decision %s action=%s outcome=%s mode %s->%s level %s->%s reasons=%s",
            rec.decision_id, action, outcome, before, self._mode,
            level_before, self._level, reasons,
        )
        return rec

    # ---------- 对外入口 ----------

    def evaluate(self, snapshot: SystemSnapshot) -> DecisionRecord:
        """根据快照评估并应用一次调度决策（自动调度入口）。

        幂等：同一快照反复评估得到同一目标档位，不会叠加削减量。
        """
        with self._lock:
            errors = self._snapshot_errors(snapshot)
            if errors:
                return self._record(
                    snapshot, LoadAction.NONE,
                    ["invalid snapshot: " + "; ".join(errors)],
                    DecisionOutcome.INVALID_INPUT,
                )
            loads, load_errors = self._collect_loads()
            if load_errors:
                return self._record(
                    snapshot, LoadAction.NONE,
                    ["invalid load config: " + "; ".join(load_errors)],
                    DecisionOutcome.INVALID_LOAD_CONFIG,
                )
            action, target, outcome, reasons = self._decide(snapshot, loads)
            return self._record(snapshot, action, reasons, outcome,
                                target=target, loads=loads)

    def command(self, cmd: str, snapshot: SystemSnapshot) -> DecisionRecord:
        """手动命令（shed/restore）。

        并发冲突命令在锁内串行：后到命令若与当前模式一致则为确定性 no-op，
        同一时刻至多产生一次真实的模式迁移。
        """
        with self._lock:
            if cmd not in ("shed", "restore"):
                raise ValueError(f"unknown command: {cmd}")
            errors = self._snapshot_errors(snapshot)
            if errors:
                return self._record(
                    snapshot, LoadAction.NONE,
                    [f"command {cmd} rejected: invalid snapshot: "
                     + "; ".join(errors)],
                    DecisionOutcome.INVALID_INPUT,
                )
            if cmd == "restore":
                # 手动解除不依赖负载配置：直接归还全部已削减负载
                if self._mode == SystemMode.NORMAL and not self._sheds:
                    return self._record(
                        snapshot, LoadAction.NONE,
                        ["command restore: already in mode normal, no-op"],
                        DecisionOutcome.OK, target=0, loads=[],
                    )
                loads, _ = self._collect_loads()
                return self._record(
                    snapshot, LoadAction.RESTORE,
                    ["manual command: restore"],
                    DecisionOutcome.OK, target=0, loads=loads,
                )
            # shed：与自动评估同一套目标档位计算
            if self._mode == SystemMode.SHEDDING:
                return self._record(
                    snapshot, LoadAction.NONE,
                    [f"command shed: already in mode {self._mode}, no-op"],
                    DecisionOutcome.OK, target=self._level,
                    loads=self._collect_loads()[0],
                )
            loads, load_errors = self._collect_loads()
            if load_errors:
                return self._record(
                    snapshot, LoadAction.NONE,
                    ["invalid load config: " + "; ".join(load_errors)],
                    DecisionOutcome.INVALID_LOAD_CONFIG,
                )
            target, reached = self._escalation_target(
                snapshot.net_load_kw, loads)
            outcome = (DecisionOutcome.OK if reached
                       else DecisionOutcome.INSUFFICIENT_CAPACITY)
            return self._record(
                snapshot, LoadAction.SHED,
                [f"manual command: shed -> target level {target}"],
                outcome, target=target, loads=loads,
            )

    def decisions(self) -> list[DecisionRecord]:
        """全部决策历史（可追溯）。"""
        return list(self._decisions)
