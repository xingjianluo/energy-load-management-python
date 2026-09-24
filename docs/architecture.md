# 架构、数据语义与一致性保证

本服务是一个**完全本地、无外部依赖**的能源监测与负载调度组件：
多设备遥测写入一条只追加的本地事件日志（JSONL），当前状态与调度
结论全部由事件日志派生/重放得到。无数据库、无网络服务、无真实凭据。

## 1. 模块结构

```
main.py                 FastAPI 入口（create_app），保留 GET / 兼容
energy/
  config.py             配置项与环境变量加载、阈值一致性校验
  clock.py              可注入时钟（SystemClock / 测试用 FakeClock）
  errors.py             归一化错误码与异常类型
  models.py             领域模型与 API 出入参（pydantic / dataclass）
  devices.py            设备与指标注册表（单位、数值范围、可控性）
  store.py              只追加 JSONL 事件日志（fsync、崩溃恢复、锁）
  telemetry.py          逐点校验、幂等、乱序/过期/未来判定
  state.py              由事件重放派生当前系统快照
  scheduler.py          阈值滞回状态机、手动覆盖、命令冲突消解、决策留痕
  service.py            门面：统一并发临界区、错误归一化、重放装配
  app.py                FastAPI 应用工厂
api/
  routes.py             HTTP 路由
  serialization.py      领域对象 -> 固定 JSON 结构
  errors.py             异常 -> 稳定 JSON 响应
tests/                  56 个单测（遥测/调度/并发/API/持久化/兼容）
```

## 2. 数据语义

### 2.1 设备与指标
内置设备（`energy/devices.py`，可整体替换注册表）：

| 设备 | 类型 | 指标 | 单位 | 范围 |
|---|---|---|---|---|
| battery-001 | battery | `soc` | `fraction` | 0~1 |
| battery-001 | battery | `power_w` | `W` | 负=放电，正=充电 |
| solar-001 | solar | `power_w` | `W` | >=0 |
| grid-001 | grid | `power_w` | `W` | 正=取电，负=反送 |
| load-001 | load（可控） | `power_w` | `W` | >=0 |
| load-002 | load（不可控） | `power_w` | `W` | >=0 |

- 单位必须**精确等于**该指标声明的标准单位（不做静默单位换算），
  否则返回 `illegal_unit`。
- 数值必须落在指标声明范围内，否则 `value_out_of_range`；
  `NaN`/`Inf` 为 `malformed_value`。
- 未知设备 `unknown_device`、未知指标 `unknown_metric`，
  空白必填字段 `missing_field`，均逐点拒绝且不入库。

### 2.2 时间戳
- 必须带时区偏移；朴素时间戳以 `bad_timestamp` 拒绝（避免时区猜测）。
- `now - past_tolerance_seconds` 更早：`stale_timestamp`（默认 3600s）。
- `now + future_tolerance_seconds` 更晚：`future_timestamp`（默认 30s）。
- 对同一 `(device_id, metric)`，事件时间戳早于已接受最新值：
  `out_of_order`。乱序点不会覆盖新值，状态始终以最新接受点为准。
- 边界按闭区间处理：恰好等于容差/阈值边界不算越界。

### 2.3 幂等
每个点有一个去重键 `dedupe_key`：
- 优先用客户端 `client_event_id`；
- 否则取 `sha256(device_id, metric, value, unit, ts)` 的规范化哈希。

命中历史已接受键时返回 `duplicate` 和原 `event_id`，**不写新事件、
不改最新值、不产生新决策**。同批重复提交与跨重启重复提交结论一致。

### 2.4 汇总量
- `load_power_w` = 所有负载设备最新 power_w 之和；
- `solar_generation_w` = 所有光伏设备最新 power_w 之和；
- 电池放电（power_w<0）按其绝对值抵消净负载；
- `net_load_w = 负载 - 光伏 - 电池放电量`；
- 缺失的序列按"无读数"处理（不臆造为 0 参与判断，除非显式参与求和时
  以 0 处理，且某类完全无数据时汇总量为 `null`）。

## 3. 状态与调度规则（滞回）

两个互相独立的滞回锁存，加一个手动覆盖位：

- **过载锁存**：`net_load_w >= curtail_trigger_w`（默认 5000W）置位；
  `net_load_w <= curtail_release_w`（默认 3000W）清零；
  落在 (3000, 5000) 区间时**保持原状态**。
- **低电量锁存**：`soc <= soc_low`（默认 0.2）置位；
  `soc >= soc_high`（默认 0.8）清零；中间区间保持。
- 任一锁存置位或手动 `curtail` 生效 => `curtailed=true`，
  对可控负载施加 `load_limit_w`（默认 3000W，可被 `set_limit` 覆盖）。

滞回保证触发与解除不会在单一阈值附近来回抖动。

手动命令：
- `curtail`：置手动覆盖，立即限载。
- `release`：若此刻任一**进入条件仍稳固成立**（net>=trigger 或
  soc<=low），返回 `conflict` 拒绝解除（安全优先）；否则清除手动覆盖
  并复位自动锁存——锁存再次置位必须**重新越过触发阈值**，因此手动
  解除与自动触发之间不会互相抖动。
- `set_limit`：更新限载值；目标负载未知或不可控时 `rejected`。
- 命令以 `command_id` 幂等：并发或重复提交只有第一次生效，其余
  `duplicate`，返回相同决策。

每次自动评估与每条生效命令产生一条 `Decision`，记录：结论
（curtailed/limit_w/trigger）、逐条判定依据 `reasons[]`、当时输入
快照、所用阈值、内部锁存状态。决策 `decision_id = dec-<seq>`，
可逐条追溯到输入快照与规则。

## 4. 持久化、恢复与一致性

- 事件分三类：`telemetry` / `command` / `decision`，统一 append 到
  `<data_dir>/<event_log_file>`，每行一条 JSON，写入后 flush+fsync。
- seq 单调递增，是全部事件的全局全序。
- 启动时重放：遥测事件重建物理状态；命令事件重建命令去重表；
  决策事件重建滞回锁存/手动覆盖/自定义限值。
- **不重复计量**：只有 fsync 成功的事件才算已接受并参与重放；
  重启不会再次执行已接受命令（命令结果随 command 事件持久化）。
- **不丢已接受事件**：若日志最后一行因崩溃被截断（无法解析），
  只把文件截回最后一个完整行（torn-tail 修复）；完整行不动。
- **部分失败不污染整体**：拒绝的遥测点从不入库；点之间互不影响。
  遥测逐条提交，决策记录不改变物理读数（即使缺失，物理状态仍可由
  遥测日志完整重放）。

### 并发模型
所有写操作（遥测批次、命令）与状态派生都在事件存储的同一把 `RLock`
临界区内串行执行；读操作取同一把锁获得一致快照。因此：
- 并发批次不丢点、不串号（seq 连续唯一）；
- 冲突命令按进入临界区的顺序（=seq）排成全序，最终状态唯一确定，
  不存在两个命令各写一半；
- 相同 command_id / 相同批次在并发下也只有一次生效。

## 5. 兼容范围

- 保留 `GET /` -> `{"Hello":"World"}`，既有调用方不受影响。
- 请求 JSON 中出现**未知的额外字段**将被忽略，不影响既有字段处理，
  也不改变 `/state`、`/events`、`/decisions` 的固定输出结构。
- 未知的**新指标/新设备**不做静默接受（返回可区分拒绝原因），但不会
  破坏同批其他点或任何既有查询。
- 未来新增字段只会以"追加"方式出现在 payload 中；读取端对缺失字段
  保持容忍。
- 未预期内部异常统一归一化为 `internal_error`，对外只给通用文案，
  细节进服务端日志，不泄露堆栈、文件路径等实现细节。

## 6. 配置项

通过 `ENERGY_<FIELD_UPPER>` 环境变量配置（见 `energy/config.py`），
启动时做一致性校验（trigger 必须大于 release，soc_low < soc_high）：

| 配置 | 默认 | 说明 |
|---|---|---|
| `ENERGY_DATA_DIR` | `.data` | 事件日志目录 |
| `ENERGY_EVENT_LOG_FILE` | `events.jsonl` | 日志文件名 |
| `ENERGY_PAST_TOLERANCE_SECONDS` | 3600 | 早于此差值判为过期 |
| `ENERGY_FUTURE_TOLERANCE_SECONDS` | 30 | 晚于此差值判为未来 |
| `ENERGY_CURTAIL_TRIGGER_W` | 5000 | 进入限载阈值（瓦） |
| `ENERGY_CURTAIL_RELEASE_W` | 3000 | 解除限载阈值（瓦） |
| `ENERGY_SOC_LOW` | 0.2 | 低电量触发 |
| `ENERGY_SOC_HIGH` | 0.8 | 低电量解除 |
| `ENERGY_LOAD_LIMIT_W` | 3000 | 限载时施加的功率上限 |

## 7. 本地验证

见 [README.md](../README.md) 的"本地运行与验证"一节。
