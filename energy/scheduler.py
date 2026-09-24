"""负载调度：阈值滞回判定、手动覆盖与命令冲突消解。

状态机（两个独立滞回锁存 + 手动覆盖）：
- overload 锁存：net_load_w >= trigger 置位；net_load_w <= release 清零；
  处于 (release, trigger) 区间时保持原值（滞回，防止抖动）。
- low_soc 锁存：soc <= soc_low 置位；soc >= soc_high 清零；
  中间区间保持。
- 手动 CURTAIL：manual_curtail=True，立即限载。
- 手动 RELEASE：仅当"此刻任一进入条件仍成立"时以 conflict 拒绝
  （安全条件仍在，不允许手动解除）；否则清除手动覆盖并复位锁存，
  锁存再次置位必须重新越过触发阈值，故不会与触发互相抖动。
- SET_LIMIT：自定义限载功率上限；并发命令按全局 seq 串行，
  seq 较大者生效（last-writer-wins），结果唯一确定。
- 命令以 command_id 幂等：重复提交返回 DUPLICATE 与原结果，不改状态。

每次评估产生一条 Decision（含输入快照、阈值、判定依据），作为
DECISION 事件持久化；重启重放最后一条决策即可恢复锁存状态。
"""

import hashlib
import logging

from energy.clock import Clock
from energy.config import Settings
from energy.devices import DeviceRegistry
from energy.errors import ValidationAppError
from energy.models import (
    CommandIn,
    CommandResult,
    CommandStatus,
    Decision,
    Event,
    EventKind,
    SystemSnapshot,
)
from energy.store import EventStore

logger = logging.getLogger("energy.scheduler")


def _cmd_dedupe_key(command_id: str) -> str:
    return "cmd-" + command_id.strip()


class LoadScheduler:
    def __init__(
        self,
        settings: Settings,
        clock: Clock,
        registry: DeviceRegistry,
        store: EventStore,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._registry = registry
        self._store = store
        # 滞回锁存
        self._latch_overload = False
        self._latch_low_soc = False
        self._manual_curtail = False
        self._custom_limit_w: float | None = None
        # 已处理命令结果（command_id -> (CommandResult, payload)）
        self._commands: dict[str, dict] = {}
        # 决策历史（从事件重放恢复）
        self._decisions: list[Decision] = []
        self._replay()

    # ------------------------------------------------------------------
    def _replay(self) -> None:
        for event in sorted(self._store.all_events(), key=lambda e: e.seq):
            if event.kind is EventKind.COMMAND and event.dedupe_key:
                self._commands[event.dedupe_key] = event.payload
            elif event.kind is EventKind.DECISION:
                state = event.payload.get("state", {})
                self._latch_overload = bool(state.get("latch_overload", False))
                self._latch_low_soc = bool(state.get("latch_low_soc", False))
                self._manual_curtail = bool(state.get("manual_curtail", False))
                self._custom_limit_w = state.get("custom_limit_w")
                self._decisions.append(
                    Decision(
                        decision_id=str(event.payload.get("decision_id", f"dec-{event.seq}")),
                        decided_at=event.received_at,
                        curtailed=bool(event.payload.get("curtailed")),
                        limit_w=event.payload.get("limit_w"),
                        trigger=str(event.payload.get("trigger", "")),
                        reasons=list(event.payload.get("reasons", [])),
                        snapshot=dict(event.payload.get("snapshot", {})),
                        thresholds=dict(event.payload.get("thresholds", {})),
                    )
                )

    def decisions(self) -> list[Decision]:
        return list(self._decisions)

    # ------------------------------------------------------------------
    def _snapshot_inputs(self, snap: SystemSnapshot) -> dict:
        # 固定字段集合；即使后续新增指标，历史决策记录仍按当时字段可读
        return {
            "net_load_w": snap.net_load_w,
            "load_power_w": snap.load_power_w,
            "solar_generation_w": snap.solar_generation_w,
            "battery_soc": snap.battery_soc,
            "generated_at": snap.generated_at.isoformat(),
        }

    def _thresholds(self) -> dict:
        return {
            "curtail_trigger_w": self._settings.curtail_trigger_w,
            "curtail_release_w": self._settings.curtail_release_w,
            "soc_low": self._settings.soc_low,
            "soc_high": self._settings.soc_high,
            "load_limit_w": self._settings.load_limit_w,
        }

    def _entry_conditions(self, snap: SystemSnapshot) -> list[str]:
        """此刻是否仍有"进入条件"稳固成立（用于拒绝手动 RELEASE）。"""
        held: list[str] = []
        if snap.net_load_w is not None and snap.net_load_w >= self._settings.curtail_trigger_w:
            held.append(
                f"net_load_w={snap.net_load_w} >= trigger={self._settings.curtail_trigger_w}"
            )
        if snap.battery_soc is not None and snap.battery_soc <= self._settings.soc_low:
            held.append(f"battery_soc={snap.battery_soc} <= soc_low={self._settings.soc_low}")
        return held

    # ------------------------------------------------------------------
    def evaluate(self, snap: SystemSnapshot) -> Decision:
        """依据当前快照推进滞回锁存并持久化一条决策。调用方持 store.lock。"""
        reasons: list[str] = []

        # --- overload 锁存（滞回） ---
        net = snap.net_load_w
        if net is None:
            reasons.append("net_load_w unavailable; overload latch retained")
        elif net >= self._settings.curtail_trigger_w:
            if not self._latch_overload:
                reasons.append(
                    f"net_load_w={net} >= trigger={self._settings.curtail_trigger_w} -> overload SET"
                )
            else:
                reasons.append(f"net_load_w={net} still >= trigger; overload remains SET")
            self._latch_overload = True
        elif net <= self._settings.curtail_release_w:
            if self._latch_overload:
                reasons.append(
                    f"net_load_w={net} <= release={self._settings.curtail_release_w} -> overload CLEAR"
                )
            else:
                reasons.append(f"net_load_w={net} <= release; overload remains clear")
            self._latch_overload = False
        else:
            reasons.append(
                f"net_load_w={net} in hysteresis band ("
                f"{self._settings.curtail_release_w}, {self._settings.curtail_trigger_w}); latch retained="
                f"{self._latch_overload}"
            )

        # --- low_soc 锁存（滞回） ---
        soc = snap.battery_soc
        if soc is None:
            reasons.append("battery_soc unavailable; low_soc latch retained")
        elif soc <= self._settings.soc_low:
            if not self._latch_low_soc:
                reasons.append(f"battery_soc={soc} <= soc_low={self._settings.soc_low} -> low_soc SET")
            else:
                reasons.append("battery_soc still <= soc_low; low_soc remains SET")
            self._latch_low_soc = True
        elif soc >= self._settings.soc_high:
            if self._latch_low_soc:
                reasons.append(f"battery_soc={soc} >= soc_high={self._settings.soc_high} -> low_soc CLEAR")
            else:
                reasons.append("battery_soc >= soc_high; low_soc remains clear")
            self._latch_low_soc = False
        else:
            reasons.append(
                f"battery_soc={soc} in hysteresis band ({self._settings.soc_low}, {self._settings.soc_high}); "
                f"latch retained={self._latch_low_soc}"
            )

        if self._manual_curtail:
            reasons.append("manual_curtail override active")

        curtailed = self._latch_overload or self._latch_low_soc or self._manual_curtail
        if curtailed:
            trigger = "manual_curtail" if self._manual_curtail else "auto"
            limit_w = (
                self._custom_limit_w
                if self._custom_limit_w is not None
                else self._settings.load_limit_w
            )
        else:
            trigger = "clear"
            limit_w = None
            reasons.append("no latch/override active -> curtailment CLEAR")

        state = {
            "latch_overload": self._latch_overload,
            "latch_low_soc": self._latch_low_soc,
            "manual_curtail": self._manual_curtail,
            "custom_limit_w": self._custom_limit_w,
        }
        # decision_id 绑定即将分配的单调 seq；重放时从 payload 原样恢复，
        # 保证重启后决策历史逐字段一致。
        decision_id = f"dec-{self._store.peek_next_seq()}"
        payload = {
            "decision_id": decision_id,
            "curtailed": curtailed,
            "limit_w": limit_w,
            "trigger": trigger,
            "reasons": reasons,
            "snapshot": self._snapshot_inputs(snap),
            "thresholds": self._thresholds(),
            "state": state,
        }
        event = self._store.append_new(
            event_id=decision_id,
            kind=EventKind.DECISION,
            occurred_at=snap.generated_at,
            device_id=None,
            metric=None,
            value=None,
            unit=None,
            dedupe_key=None,
            payload=payload,
        )
        assert f"dec-{event.seq}" == decision_id
        decision = Decision(
            decision_id=decision_id,
            decided_at=event.received_at,
            curtailed=curtailed,
            limit_w=limit_w,
            trigger=trigger,
            reasons=reasons,
            snapshot=payload["snapshot"],
            thresholds=payload["thresholds"],
        )
        self._decisions.append(decision)
        logger.info(
            "decision %s curtailed=%s limit_w=%s latch(ov=%s,soc=%s) manual=%s",
            decision.decision_id,
            curtailed,
            limit_w,
            self._latch_overload,
            self._latch_low_soc,
            self._manual_curtail,
        )
        for line in reasons:
            logger.info("  basis: %s", line)
        return decision

    # ------------------------------------------------------------------
    def apply_command(self, command: CommandIn, snap: SystemSnapshot) -> CommandResult:
        """处理手动命令；调用方持 store.lock。"""
        key = _cmd_dedupe_key(command.command_id)
        if not command.command_id.strip():
            raise ValidationAppError("command_id must not be empty")

        prior = self._commands.get(key)
        if prior is not None:
            logger.info(
                "command %s DUPLICATE of prior result=%s",
                command.command_id,
                prior.get("status"),
            )
            return CommandResult(
                command_id=command.command_id,
                status=CommandStatus.DUPLICATE,
                decision_id=prior.get("decision_id"),
                reason="duplicate_command",
                applied_at=None,
            )

        status = CommandStatus.APPLIED
        reason: str | None = None
        decision_id: str | None = None

        if command.command_type.value == "curtail":
            self._manual_curtail = True
            reason = "manual curtail applied"
        elif command.command_type.value == "release":
            held = self._entry_conditions(snap)
            if held:
                # 安全条件仍稳固成立：拒绝解除，避免释放/触发来回抖动
                status = CommandStatus.CONFLICT
                reason = "release refused; entry conditions still hold: " + "; ".join(held)
            else:
                self._manual_curtail = False
                # 复位自动锁存；重新置位必须重新越过触发阈值
                self._latch_overload = False
                self._latch_low_soc = False
                reason = "manual release applied; latches reset (re-entry requires crossing trigger)"
        elif command.command_type.value == "set_limit":
            limit = command.limit_w
            if limit is None or not isinstance(limit, (int, float)) or isinstance(limit, bool):
                raise ValidationAppError("set_limit requires numeric limit_w >= 0")
            if limit < 0:
                raise ValidationAppError("limit_w must be >= 0")
            if command.target_load_id is not None:
                spec = self._registry.get(command.target_load_id)
                if spec is None or not spec.controllable:
                    status = CommandStatus.REJECTED
                    reason = f"load {command.target_load_id!r} unknown or not controllable"
                else:
                    self._custom_limit_w = float(limit)
                    reason = f"custom limit {float(limit)} W for {command.target_load_id}"
            else:
                self._custom_limit_w = float(limit)
                reason = f"custom limit {float(limit)} W (all controllable loads)"
        else:  # pragma: no cover - 枚举已约束
            raise ValidationAppError(f"unknown command type {command.command_type}")

        event = self._store.append_new(
            event_id="cmd-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32],
            kind=EventKind.COMMAND,
            occurred_at=self._clock.now(),
            device_id=command.target_load_id,
            metric=None,
            value=command.limit_w,
            unit="W",
            dedupe_key=key,
            payload={
                "command_id": command.command_id,
                "command_type": command.command_type.value,
                "status": status.value,
                "reason": reason,
                "limit_w": command.limit_w,
            },
        )

        if status is CommandStatus.APPLIED:
            decision = self.evaluate(snap)
            decision_id = decision.decision_id

        result = CommandResult(
            command_id=command.command_id,
            status=status,
            decision_id=decision_id,
            reason=reason,
            applied_at=self._clock.now() if status is CommandStatus.APPLIED else None,
        )
        self._commands[key] = {
            "status": status.value,
            "decision_id": decision_id,
            "reason": reason,
        }
        logger.info(
            "command %s type=%s -> %s decision_id=%s",
            command.command_id,
            command.command_type.value,
            status.value,
            decision_id,
        )
        return result
