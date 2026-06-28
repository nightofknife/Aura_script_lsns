# Observability

Aura 当前通过 `ObservabilityService` 跟踪任务、节点、队列和运行指标。

## 1. 事件来源

核心事件族：

- `task.started`
- `task.finished`
- `node.started`
- `node.succeeded`
- `node.failed`
- `node.skipped`
- `node.finished`
- `queue.enqueued`
- `queue.dequeued`
- `queue.completed`

这些事件会进入：

- `EventBus`
- `ObservabilityService`
- `RunStore`
- `ui_event_queue`

## 2. 运行快照

observability 维护多类运行集合：

- active runs
- ready runs
- delayed runs
- completed runs
- trace id 到 cid 映射

## 3. completed run 与 TTL

相关配置：

- `observability.completed_task_ttl`
- `observability.cleanup_interval`
- `observability.max_completed_tasks`

完成任务不会立刻消失，会由后台 cleanup task 定期清理。

## 4. 指标

常见 metrics：

- `tasks_started`
- `tasks_finished`
- `tasks_success`
- `tasks_error`
- `tasks_failed`
- `tasks_timeout`
- `tasks_cancelled`
- `tasks_running`
- `nodes_total`
- `nodes_succeeded`
- `nodes_failed`
- `nodes_duration_ms_sum`
- `nodes_duration_ms_avg`
- `updated_at`

## 5. UI 事件队列

`ui_event_queue` 是 event bus 的镜像输出，用于：

- TUI
- 桌面 UI
- 未来外部消费者

## 6. RunStore

`ObservabilityService` 会把事件同步应用到 `RunStore`，用于：

- 保留时间线数据
- 支持基于 `cid` 或 `trace_id` 查询
- 支持持久化运行历史

## 7. 当前对外查询能力

scheduler 当前公开了若干查询接口：

- `get_metrics_snapshot()`
- `get_active_runs_snapshot()`
- `list_persisted_runs()`
- `get_persisted_run()`
- `get_run_timeline()`
- `get_batch_task_status()`

拆分项目里，这些能力主要通过本地 SDK 和 CLI 暴露，而不是 HTTP 接口。

## 8. 排障建议

- 看不到任务进度：先确认 `task.started` / `task.finished` 是否正常发布
- 运行历史缺失：检查 TTL 清理和 persist 配置
- `trace_id` 找不到对应 run：先确认 `task.started` 是否进入 `RunStore`
