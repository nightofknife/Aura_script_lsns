# Scheduler 与 Runtime Profile

拆分后的项目保留了原有调度器与 profile 机制，但运行入口已经从 HTTP 服务切回了本地 CLI 和 Python SDK。

## 1. Scheduler singleton

当前只有一层核心 singleton：

- `packages.aura_core.runtime.bootstrap`
  负责创建、启动和停止运行时中的 `Scheduler`

`EmbeddedGameRunner` 和 `cli.py` 都直接使用这层 runtime singleton。

## 2. 启动与停止

### `create_runtime(profile=...)`

- 只创建 runtime / scheduler
- 不启动 control loop
- 适合列出游戏模块、任务元数据、做静态检查

### `start_runtime(profile=...)`

- 创建或复用 runtime
- 启动 scheduler 主线程与 control loop
- 等待 `startup_complete_event`

### `stop_runtime()`

- 停止 scheduler
- 释放 runtime singleton

在拆分项目里，`packages.aura_game.EmbeddedGameRunner` 还会额外清理全局 action/service/hook 注册表，避免重复启动时残留旧状态。

## 3. 内置 Runtime Profile

当前保留三个 profile 名称：

### `embedded_full`

- `enable_schedule_loop = true`
- `enable_interrupt_loop = true`
- `enable_event_triggers = true`

这是拆分项目默认的本地 SDK / CLI profile。

### `api_full`

- 与 `embedded_full` 行为相同
- 仅作为兼容别名保留

### `tui_manual`

- `enable_schedule_loop = false`
- `enable_interrupt_loop = false`
- `enable_event_triggers = false`

适合：

- 手动任务执行
- 本地调试
- 避免后台自动调度干扰

## 4. pre-start buffer

如果 scheduler 尚未启动，ad-hoc task 会先进入 `_pre_start_task_buffer`，待 runtime loop 启动后再 flush 到 `task_queue`。

拆分项目里的 `EmbeddedGameRunner.run_task()` 和 `SubprocessGameRunner.run_task()` 默认会先确保 runtime 已启动，因此日常使用通常不会落入这个缓冲区。

## 5. 当前建议

- 本地脚本、测试、批量任务：优先用 `EmbeddedGameRunner`
- 宿主 GUI 或桌面程序集成：优先用 `SubprocessGameRunner`
- 人工触发和交互式调试：用 `cli.py tui`
