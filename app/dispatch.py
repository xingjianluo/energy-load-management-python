"""负载调度：带滞回的状态机，决策可追溯，并发冲突只有一个确定结果。

滞回规则（net_load = load_kw - generation_kw）：
- NORMAL 下 net_load > shed_threshold 或 soc < storage_low_soc → 触发限载(SHED)
- SHEDDING 下 net_load < restore_threshold 且 soc >= storage_low_soc → 解除限载(RESTORE)
- 两阈值之间为滞回带：保持当前模式，不抖动。
所有状态变更在同一把锁内完成：并发冲突命令被串行化，
后到的命令看到已生效的模式，结果为确定性的 no-op。
"""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import UTC, datetime
from typing import Any, Callable

from app.config import Settings
from app.models import (
    DecisionRecord,
    LoadAction,
    SystemMode,
    SystemSnapshot,
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
        self._decisions: list[DecisionRecord] = []
        path = decision_store_path or (
            settings.store_path + ".decisions.jsonl" if settings.store_path else None
        )
        self._store = EventStore(path) if path else EventStore(None)
        # 重启恢复：重放决策历史，恢复最后模式，不重复记录
        for ev in self._store.all_events():
            rec = DecisionRecord.model_validate(ev["record"])
            self._decisions.append(rec)
            self._mode = rec.mode_after

    @property
    def mode(self) -> SystemMode:
        return self._mode

    def _decide(self, snapshot: SystemSnapshot) -> tuple[LoadAction, list[str]]:
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

    def _record(self, snapshot: SystemSnapshot, action: LoadAction,
                reasons: list[str]) -> DecisionRecord:
        before = self._mode
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
        )
        self._decisions.append(rec)
        self._store.append({"event_id": rec.decision_id,
                            "record": rec.model_dump(mode="json")})
        logger.info(
            "decision %s action=%s mode %s->%s reasons=%s",
            rec.decision_id, action, before, self._mode, reasons,
        )
        return rec

    def evaluate(self, snapshot: SystemSnapshot) -> DecisionRecord:
        """根据快照评估并应用决策（自动调度入口）。"""
        with self._lock:
            action, reasons = self._decide(snapshot)
            return self._record(snapshot, action, reasons)

    def command(self, cmd: str, snapshot: SystemSnapshot) -> DecisionRecord:
        """手动命令（shed/restore）。

        并发冲突命令在锁内串行：后到命令若与当前模式一致则为确定性 no-op，
        同一时刻至多产生一次真实的模式迁移。
        """
        with self._lock:
            target = {
                "shed": SystemMode.SHEDDING,
                "restore": SystemMode.NORMAL,
            }.get(cmd)
            if target is None:
                raise ValueError(f"unknown command: {cmd}")
            if target == self._mode:
                return self._record(
                    snapshot, LoadAction.NONE,
                    [f"command {cmd}: already in mode {self._mode}, no-op"],
                )
            action = LoadAction.SHED if target == SystemMode.SHEDDING else LoadAction.RESTORE
            return self._record(
                snapshot, action, [f"manual command: {cmd}"]
            )

    def decisions(self) -> list[DecisionRecord]:
        """全部决策历史（可追溯）。"""
        return list(self._decisions)
