# 能源监测与负载调度（energy-load-management）

基于 FastAPI 的本地可复现能源监测与负载调度服务：汇总多设备遥测、储能状态与
可控负载，在乱序、重复、异常数据及并发操作下保持结论一致且可解释。
不依赖任何外部服务、真实凭据或不可访问资源。

## 本地运行与验证

```bash
uv sync                 # 安装依赖
uv run uvicorn main:app # 启动服务（默认 :8000）
uv run pytest -q        # 运行全部测试
uv run pytest -q -s     # 查看单测日志（原始输入、判定依据、关键状态变化）
```

## 数据语义

### 遥测记录（POST /telemetry，单条对象或数组）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `event_id` | string | 幂等键，全局唯一 |
| `device_id` | string | 必须已注册（POST /devices 或 `ELM_KNOWN_DEVICES`） |
| `metric` | string | `power` / `energy` / `voltage` / `current` / `soc` / `temperature` |
| `value` | number | 数值（布尔、字符串一律拒绝） |
| `unit` | string | 必须为该指标的合法单位，见下表 |
| `timestamp` | ISO8601 或 epoch 秒 | 无时区按 UTC 处理 |

合法单位与归一化：`W→kW`、`Wh→kWh`、`percent→ratio`（0-1），其余原样。
未知扩展字段原样保留在事件中，不参与校验（前向兼容）。

### 处理结果（稳定且可区分）

每条记录独立判定，返回 `accepted` / `duplicate` / `rejected`；
拒绝原因：`missing_field`、`unknown_device`、`unknown_metric`、`invalid_unit`、
`invalid_value`、`stale_timestamp`、`future_timestamp`、`out_of_order`。

校验顺序固定：幂等去重 → 必填字段 → 字段类型 → 设备 → 指标 → 单位 →
未来时间戳 → 过期时间戳 → 乱序。同一输入永远得到同一结果。

- **乱序**：同一 `(device_id, metric)` 只接受时间戳严格递增的记录。
- **过期/未来**：超过 `max_stale_seconds` 或 `max_future_skew_seconds` 拒绝。
- **重复**：`event_id` 已存在即 `duplicate`，不触碰任何状态。

## 状态与调度规则

快照（GET /snapshot）：`load_kw`（load 设备功率和）、`generation_kw`
（generator 功率和）、`storage_soc`（storage 设备最小 soc）、
`net_load_kw = load_kw - generation_kw`。

调度状态机（POST /dispatch/evaluate，或 POST /dispatch/command 手动）：

- `NORMAL`：`net_load > shed_threshold_kw` **或** `soc < storage_low_soc` → `SHED`，进入 `SHEDDING`
- `SHEDDING`：`net_load < restore_threshold_kw` **且** `soc >= storage_low_soc` → `RESTORE`，回到 `NORMAL`
- 两阈值之间为**滞回带**：保持当前模式，解除限载不会与触发条件互相抖动
- 边界为严格比较：等于阈值不迁移

每次决策生成 `DecisionRecord`（GET /dispatch/decisions）：输入快照、
判定依据（reasons）、当时阈值、模式迁移，完全可追溯。

## 幂等与并发保证

- 同一批数据重复提交：结果从 `accepted` 变为 `duplicate`，状态与历史口径不变。
- 所有状态变更在锁内完成：同一 `event_id` 并发提交恰好一个 `accepted`；
  冲突命令（shed/restore）并发到达时被串行化，只产生一个确定的模式迁移，
  后到命令得到确定性的 no-op。
- 持久化：`ELM_STORE_PATH` 设置后，事件与决策追加写入 JSONL（写后 fsync）。
  重启重放不重复计量、不丢失已接受事件；损坏行被跳过，不污染整体状态。
  部分写入失败抛 `store_unavailable`，内存状态不被污染。

## 错误归一化

所有内部异常映射为稳定 JSON：`{"error": {"code", "message"}}`，
不泄露实现细节。错误码：`validation_error`(422)、`conflict`(409)、
`not_found`(404)、`store_unavailable`(503)、`internal_error`(500)。

## 兼容范围

- 既有 `GET /` 行为不变（`{"Hello": "World"}`）。
- 未知新指标：拒绝并给出 `unknown_metric`，不影响既有数据与查询。
- 未知新字段：原样保留，历史与快照查询不受影响。
- 存储格式为追加式 JSONL，旧版本文件可直接重放。

## 配置项（环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ELM_MAX_FUTURE_SKEW_SECONDS` | 60 | 未来时间戳容忍 |
| `ELM_MAX_STALE_SECONDS` | 3600 | 过期时间戳容忍 |
| `ELM_SHED_THRESHOLD_KW` | 10.0 | 触发限载阈值 |
| `ELM_RESTORE_THRESHOLD_KW` | 8.0 | 解除限载阈值（滞回下沿） |
| `ELM_STORAGE_LOW_SOC` | 0.2 | 储能低电量阈值 |
| `ELM_STORE_PATH` | 空（纯内存） | 事件持久化文件路径 |
| `ELM_KNOWN_DEVICES` | 空 | 预注册设备，逗号分隔，支持 `id` 或 `id:kind`（load/generator/storage/meter，缺省 meter） |

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/devices` | 注册设备（幂等；kind 冲突返回 409） |
| GET | `/devices` | 设备列表 |
| POST | `/telemetry` | 单条/批量遥测接入 |
| GET | `/telemetry/history?device_id=` | 已接受事件历史 |
| GET | `/snapshot` | 系统实时快照 |
| GET | `/dispatch/status` | 当前模式 |
| POST | `/dispatch/evaluate` | 用当前快照评估一次调度 |
| POST | `/dispatch/command` | 手动 `shed` / `restore` |
| GET | `/dispatch/decisions` | 决策历史（可追溯） |
