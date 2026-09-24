# energy-load-management

本地可复现的能源监测与负载调度服务（FastAPI）。汇总多设备遥测、储能
状态与可控负载，在**乱序、重复、异常数据及并发操作**下给出一致、
可追溯、可解释的结论。不依赖任何外部服务、真实凭据或不可访问资源。

## 能力概览

- **多设备遥测接入 + 幂等**：重复 / 乱序 / 过期 / 未来时间戳 / 未知设备 /
  非法单位 / 缺失字段 / 越界或非数值，逐点给出稳定可区分的结论；
  同一批重复提交不改变状态与历史口径。
- **滞回负载调度**：净负载过载与储能低电量两条独立滞回状态机，
  触发与解除不抖动；手动 curtail/release/set_limit 命令可追溯，
  冲突命令在并发下只有一个确定结果。
- **一致性与恢复**：只追加事件日志 + fsync，重启重放不重复计量、
  不丢已接受事件；崩溃产生的截断尾行自动修复；内部异常归一化为
  可区分错误且不泄露实现细节；部分失败不污染整体状态。
- **兼容性**：保留 `GET /` 的 `{"Hello":"World"}`；未知额外字段不影响
  既有处理与查询结构。

更多语义见 [docs/architecture.md](docs/architecture.md)，
HTTP 细节见 [docs/api.md](docs/api.md)。

## 环境要求

- Python 3.14+（`pyproject.toml` 声明）
- 使用 [uv](https://docs.astral.sh/uv/) 管理依赖；依赖仅
  `fastapi[standard]` 与开发依赖 `pytest`，全部本地运行。

## 快速开始

```bash
uv sync                       # 安装依赖到本地 .venv

# 启动服务（默认监听 127.0.0.1:8000，事件日志写入 ./.data/events.jsonl）
uv run fastapi dev main.py

# 另一个终端：写入一批遥测
curl -s -X POST localhost:8000/api/v1/telemetry -H 'content-type: application/json' -d '{
  "batch_id":"demo",
  "points":[
    {"device_id":"load-001","metric":"power_w","value":6000,"unit":"W","ts":"2026-09-24T00:00:00Z"},
    {"device_id":"solar-001","metric":"power_w","value":0,"unit":"W","ts":"2026-09-24T00:00:00Z"},
    {"device_id":"battery-001","metric":"soc","value":0.9,"unit":"fraction","ts":"2026-09-24T00:00:00Z"}
  ]
}'

curl -s localhost:8000/api/v1/state         # 当前快照
curl -s localhost:8000/api/v1/decisions     # 调度决策与判定依据
curl -s localhost:8000/api/v1/devices       # 已知设备/指标/单位/范围
```

> 注意：示例使用固定时间戳。真实使用时请发送接近当前时间、带时区
> 偏移的 ISO-8601 时间戳，否则会被判定为过期/未来时间戳而拒绝。
> 交互式文档：`http://localhost:8000/docs`。

## 配置

通过环境变量覆盖，例如：

```bash
ENERGY_CURTAIL_TRIGGER_W=6000 ENERGY_DATA_DIR=./.data uv run fastapi dev main.py
```

全部配置项见 [docs/architecture.md](docs/architecture.md#6-配置项)。

## 本地运行与验证（测试）

```bash
uv run pytest -q                 # 运行全部单测
uv run pytest -q -o log_cli=true --log-cli-level=INFO   # 同时查看判定日志
```

测试覆盖：

- 重复提交与乱序/过期/未来/边界时间戳；
- 未知设备/指标、非法单位、缺失字段、NaN/Inf/越界值、空批次；
- 阈值边界与滞回（触发、中间区间保持、解除、低电量滞回、不抖动）；
- 命令幂等、手动解除与安全冲突、自定义限值；
- 多线程并发写入、并发冲突命令、并发重复命令/批次、并发读写；
- 崩溃截断恢复、重启重放不重复计量、决策历史逐字段一致；
- 未知额外字段前向兼容、内部错误归一化不泄露细节。

单测日志（`energy.telemetry` / `energy.state` / `energy.scheduler`）
会打印**原始输入、采用的判定依据与关键状态变化**，失败时由 pytest
的 caplog 直接展示，便于核对结论。

## 目录

```
main.py / energy/ / api/ / tests/ / docs/
```
