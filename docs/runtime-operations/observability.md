# Observability

Aura 当前通过 `ObservabilityService` 跟踪任务、节点、队列和运行指标。

## 错误现场与日志保留

GUI/CLI 启动时按 `logging.log_dir` 解析绝对日志目录（默认安装目录下的
`logs/`），创建独立会话。GUI 与任务子进程共享会话 ID，但分别持有进程租约。
日志文件追加会话 ID、PID 和随机后缀，避免同一秒启动或任务重启时混写。

最终异常或跑商明确的 `failed/error` 业务返回会尝试保存一张 PNG 到
`logs/errors/<session_id>/<evidence_id>.png`，并在文字日志中写入
`[FailureEvidence]`：截图相对路径、CID、任务/节点、阶段、错误码、时间、尺寸和
截图后端。同一错误链的截图引用会传递到节点和任务结果 `failure_evidence`，
不会因子任务包装、YAML 返回映射而再次截图；不同错误保留各自引用。

气泡水操作在异常向上传递之前取证；框架兜底和业务返回截图会标记
`scene_may_have_changed=true`，不保证是错误发生的原始画面。正常取消、控制流、
尚可重试的失败、资源不足造成的正常停止不触发截图。截图不恢复、激活、移动或
重新绑定窗口；Windows WGC/MuMu 使用现有的新鲜缓存帧，PrintWindow 使用
已有后端读取目标客户区；GDI/DXGI 无法排除桌面遮挡时跳过。
缓存不可用、目标未绑定或正在操作时保留原因，不改变原失败。

截图最多等待 2 秒；同时最多一个诊断线程。超时线程不会写文件，也不会被等待退出；
在它结束前后续截图跳过。该等待限制不代表操作系统截图或磁盘写入具有硬实时保证。

日志清理仅在 GUI/CLI 启动时尝试一次，任务子进程不清理，运行中没有定时清理。
只删除有会话元数据明确登记、最后修改时间超过 72 小时的整组日志、数字滚动备份
和对应错误截图。存在其他活跃租约、未知元数据或协调锁竞争时跳过本次清理。
符号链接/目录联接、无法确认归属的旧日志、诊断/更新器文件以及 RunStore
SQLite/WAL/SHM 均保留，历史统计不受影响。反馈问题时请提供本次日志及对应
`errors/<session_id>/` 目录；截图可能包含游戏内昵称或聊天内容，请先检查再分享。

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
