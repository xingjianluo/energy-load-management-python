# HTTP API 参考

所有接口前缀 `/api/v1`，请求/响应均为 JSON，时间戳使用带时区偏移的
ISO-8601（如 `2026-09-24T00:00:00Z` 或 `+08:00`），服务端统一按 UTC 比较。

## 错误响应

结构固定：

```json
{ "error": "<code>", "message": "<human readable>", "details": { ... } }
```

| error | HTTP | 含义 |
|---|---|---|
| `validation_error` | 422 | 请求结构/字段类型错误，`details.issues[]` 给出字段级原因 |
| `batch_empty` | 422 | 遥测批次没有任何点 |
| `conflict` | 409 | 命令与当前安全条件冲突（如触发条件仍成立时手动 release） |
| `not_found` | 404 | 资源不存在 |
| `storage_error` | 500 | 本地持久化失败 |
| `internal_error` | 500 | 归一化后的未预期内部错误（不回传内部堆栈/路径） |

遥测逐点的业务拒绝不使用 HTTP 错误，而在结果数组中给出稳定原因码：
`unknown_device` / `unknown_metric` / `illegal_unit` / `missing_field` /
`malformed_value` / `value_out_of_range` / `bad_timestamp` /
`stale_timestamp` / `future_timestamp` / `out_of_order`，重复点为
`status=duplicate`。

## `GET /`
保持骨架既有行为：`{"Hello": "World"}`。

## `GET /api/v1/health`
`{"status":"ok"}`

## `GET /api/v1/devices`
返回已知设备、类型、是否可控、各指标的标准单位与合法数值范围。

## `POST /api/v1/telemetry`
```json
{
  "batch_id": "可选，便于对账",
  "points": [
    {
      "device_id": "load-001",
      "metric": "power_w",
      "value": 1200.5,
      "unit": "W",
      "ts": "2026-09-24T00:00:00Z",
      "client_event_id": "可选；提供时作为幂等键"
    }
  ]
}
```
响应：
```json
{
  "batch_id": "...", "accepted": 1, "duplicate": 0, "rejected": 0,
  "results": [
    {"index": 0, "status": "accepted", "reason": null,
     "event_id": "evt-...", "detail": null}
  ]
}
```
- 点之间相互独立：部分点被拒绝不会影响同批其他点，也不会污染状态
  （拒绝点从不入库）。
- 同一批原样重复提交：结果为 `duplicate`，返回相同 `event_id`，
  状态与历史口径不变。

## `GET /api/v1/state`
当前系统快照：`battery_soc`、`solar_generation_w`、`load_power_w`、
`net_load_w` 及每设备最新读数（值/单位/时间戳/来源 event_id）。

## `POST /api/v1/commands`
```json
{"command_id":"c-1","command_type":"curtail"}
```
`command_type` 取值：`curtail`（手动限载）、`release`（手动解除）、
`set_limit`（自定义限载值，需 `limit_w`，可带 `target_load_id`）。
响应：
```json
{"command_id":"c-1","status":"applied","decision_id":"dec-5",
 "reason":"manual curtail applied","applied_at":"..."}
```
`status`：`applied` / `duplicate` / `rejected` / `conflict`。
相同 `command_id` 重复提交返回 `duplicate` 与原 `decision_id`。

## `GET /api/v1/decisions`
调度决策历史（按 seq 顺序）。每条包含 `curtailed`、`limit_w`、
`trigger`、判定依据 `reasons[]`、当时的输入快照 `input_snapshot`
以及 `thresholds`，可追溯每个结论到输入与规则。

## `GET /api/v1/events?device_id=&metric=`
查看事件日志（含遥测、命令、决策），支持按设备、指标过滤。
