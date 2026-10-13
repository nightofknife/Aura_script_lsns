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

## 9. 输入与停止的运行证据

`[RuntimeTrace]` 后是 JSON。每条记录包含 `session_id`、进程 PID、`cid`、
`root_cid`、线程 ID/名称、UTC 时间与进程单调时钟；跨进程用墙钟与 CID 关联，
同一进程的耗时使用单调时钟。`operation_id` 标识一次执行，`parent_activity_id`
连接节点、动作、工作线程与输入。进程 PID 与游戏 `target_pid` 不同。

| 事件 | 含义 |
| --- | --- |
| `runtime.config` / `runtime.bound` / `input.mode` | 应用版本、实际目标 PID/HWND、截图和输入后端、激活配置、桥接配置与 GUI 覆盖值 |
| `activity.queued` / `started` / `finished` | 节点、后继调度、动作、工作线程或输入操作的生命周期；queued.fields 含参数摘要，finished 含状态和耗时 |
| `input.edge` | 实际发送的鼠标/键盘边沿及 API 返回结果；这不是游戏业务操作成功的证明 |
| `input.release_item` / `input.release_result` | 各按键释放结果与仍跟踪的按下状态；失败保留记录，允许后续释放重试 |
| `input.focus_attach` / `input.focus_detach` | Windows 输入线程关联和解除的结果 |
| `stop.requested` / `stop.observed` | 取消请求已设置，以及动作首次实际检测到取消 |
| `task.terminal` | 调度入口结束，可能仍有执行单元存活 |
| `stop.slow` / `stop.pending` | 请求超过 2 秒仍未退出，列出仍存活的执行单元及耗时 |
| `stop.drained` | 根任务已结束，已登记的节点/线程/输入活动全部退出；需另外查看释放结果，不能据此认定游戏已恢复响应 |
| `diagnostic.saved` | 诊断 JSON 的实际保存路径 |

同步 action、异步 action、DAG 节点及后继调度均登记。跑商内部的线程工作通过
`tracked_to_thread()` 登记：取消等待者不会提前注销仍在运行的线程。新加的后台工作
应使用这一入口或 `activity()`，禁止仅凭线程池里是否还有线程判断任务是否停止。
`runner.get_run()` 的 `execution_pending` 和 `pending_activities` 返回这些活动，供 GUI
保持停止中状态。这些记录用于观察真实生命周期，不强制终止 Python 线程。

### 自动诊断与日志量

- 最近 2048 条结构化事件保存在进程内存环形缓冲区。输入操作和阶段变化进入常规日志；
  高频移动、截图读取和 OCR 细节保留在缓冲区。队列与到站轮询在状态变化时记录，并每 10 秒汇总。
- 停止超过 2 秒、输入异常、释放失败或输入线程解除失败会异步请求诊断。诊断包含该任务
  最近 256 条事件、活动列表、线程调用栈（不含局部变量）、实际配置、前台窗口、光标位置、
  光标命中的窗口以及内部跟踪的按键状态。
- 截图只读取所属目标已有的 WGC 缓存帧，帧龄不超过 2 秒；不可用、忙碌、陈旧或目标归属不符
  都记录原因。不会为了诊断创建会话、切换窗口、激活游戏或执行普通截图恢复路径。
- 诊断请求有界排队（8 个），同任务同原因至少间隔 10 秒。每个提供器最多运行一个采集线程，
  等待最多 0.5 秒；超时后记录 timeout，仍忙时不重复启动采集。取证不能阻塞输入执行线程。
- 文件保存在当前日志目录的 `diagnostics/`，优先使用 `AURA_LOG_ROOT`。仅自动清理
  `aura-trace-*` 自有文件，保留 72 小时且最多 128 组；其他日志与人工文件不受影响。
- 输入正文不写入运行证据，只记录长度；密码、密钥等命名字段脱敏。目标缓存截图可能含游戏画面。

排查“停止后游戏点不了”时，先按 CID 找到 `stop.requested`，再核对是否出现新的
`input.edge`、哪些活动尚未 `finished`、释放或 detach 是否失败。若已有 `stop.drained`，
再结合诊断里的窗口和按键快照分析；不要将 `task.terminal` 当作全部工作已经退出。
