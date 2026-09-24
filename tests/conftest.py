"""测试夹具。

单测日志统一打印：原始输入、采用的判定依据、关键状态变化
（由 telemetry/scheduler/state 模块的 logger 输出）。
"""

import logging
import tempfile

import pytest

from energy.clock import FakeClock
from energy.config import Settings
from energy.service import EnergyService


@pytest.fixture(autouse=True)
def _test_logs(caplog):
    # 捕获并让 pytest 在失败时展示关键日志
    caplog.set_level(logging.INFO, logger="energy")
    yield


@pytest.fixture
def data_dir():
    with tempfile.TemporaryDirectory() as path:
        yield path


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def settings(data_dir):
    return Settings(data_dir=data_dir)


@pytest.fixture
def service(settings, clock):
    return EnergyService(settings=settings, clock=clock)


def iso(clock, offset_seconds=0.0):
    from datetime import timedelta

    return (clock.now() + timedelta(seconds=offset_seconds)).isoformat()
