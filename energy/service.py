"""门面服务：串联注册表/存储/遥测/状态/调度。

并发模型：所有写操作（遥测批次、命令）以及状态派生都在事件存储的
同一把 RLock 下串行执行；读操作也取同一把锁以拿到一致快照。
因此冲突命令并发到达时，按进入临界区的顺序（持久化 seq）产生唯一
确定结果，不存在交叉写入。

错误归一化：业务错误 AppError 原样向上抛出；未预期异常一律包装为
INTERNAL_ERROR（仅通用文案，细节进服务端日志，不对外泄露）。
"""

import functools
import logging

from energy.clock import Clock, SystemClock
from energy.config import Settings, load_settings
from energy.devices import DeviceRegistry, build_default_registry
from energy.errors import AppError, BatchEmptyAppError, ErrorCode, ValidationAppError
from energy.models import (
    BatchResultOut,
    CommandIn,
    Decision,
    Event,
    SystemSnapshot,
    TelemetryBatchIn,
)
from energy.scheduler import LoadScheduler
from energy.state import StateEngine
from energy.store import EventStore
from energy.telemetry import TelemetryService

logger = logging.getLogger("energy.service")


def _normalized(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except AppError:
            raise
        except Exception as exc:  # noqa: BLE001 - 边界归一化
            logger.exception("unexpected internal error in %s", func.__name__)
            raise AppError(
                message="internal error; request rejected without side effects",
                details={"error": ErrorCode.INTERNAL_ERROR.value},
            ) from None  # 不回传内部堆栈/实现细节

    return wrapper


class EnergyService:
    def __init__(
        self,
        settings: Settings | None = None,
        clock: Clock | None = None,
        registry: DeviceRegistry | None = None,
    ) -> None:
        self._settings = settings or load_settings()
        self._clock = clock or SystemClock()
        self._registry = registry or build_default_registry()
        self._store = EventStore(self._settings, self._clock)
        self._state = StateEngine(self._settings, self._registry)
        self._telemetry = TelemetryService(
            self._registry, self._store, self._clock, self._settings
        )
        # 从历史重放物理状态
        self._state.replay(
            [e for e in self._store.all_events()]
        )
        # 调度器重放命令/决策，恢复滞回锁存与覆盖状态
        self._scheduler = LoadScheduler(
            self._settings, self._clock, self._registry, self._store
        )

    # ------------------------------------------------------------------
    def _snapshot(self) -> SystemSnapshot:
        return self._state.snapshot(self._clock.now())

    @_normalized
    def ingest_batch(self, batch: TelemetryBatchIn) -> BatchResultOut:
        if not batch.points:
            # 空批次不得被静默接受
            raise BatchEmptyAppError(
                "telemetry batch must contain at least one point",
            )
        with self._store.lock:
            result = self._telemetry.ingest_batch(batch)
            # 仅把本轮新接受事件纳入派生状态
            for point in result.results:
                if point.status.value == "accepted" and point.event_id:
                    event = self._store.get(point.event_id)
                    if event is not None:
                        self._state.apply(event)
            # 只有真正有新接受数据时才推进调度，保证重复提交不改变历史口径
            if result.accepted > 0:
                self._scheduler.evaluate(self._snapshot())
            return result

    @_normalized
    def current_snapshot(self) -> SystemSnapshot:
        with self._store.lock:
            return self._snapshot()

    @_normalized
    def submit_command(self, command: CommandIn) -> object:
        with self._store.lock:
            return self._scheduler.apply_command(command, self._snapshot())

    @_normalized
    def list_decisions(self) -> list[Decision]:
        with self._store.lock:
            return self._scheduler.decisions()

    @_normalized
    def list_events(
        self, device_id: str | None = None, metric: str | None = None
    ) -> list[Event]:
        with self._store.lock:
            events = self._store.all_events()
        if device_id is not None:
            events = [e for e in events if e.device_id == device_id]
        if metric is not None:
            events = [e for e in events if e.metric == metric]
        return events

    def registry(self) -> DeviceRegistry:
        return self._registry


_service: EnergyService | None = None


def get_service() -> EnergyService:
    global _service
    if _service is None:
        _service = EnergyService()
    return _service


def reset_service() -> None:
    """测试支持：丢弃进程内单例（下次 get_service 重新构造并重放日志）。"""
    global _service
    _service = None
