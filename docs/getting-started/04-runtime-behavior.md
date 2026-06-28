# 运行时行为

本页补充任务 DSL 背后的执行语义，重点解释“字段如何运行”，而不是“字段怎么写”。

## 1. Task 执行过程

一个 task 的执行顺序是：

1. `Orchestrator` 读取 task YAML
2. 校验并规范化 `meta.inputs`
3. 创建 `ExecutionContext`
4. 发布 `task.started`
5. `ExecutionEngine` 构建 DAG
6. `NodeExecutor` 执行各 step
7. 汇总 `nodes`、渲染 `returns`
8. 发布 `task.finished`

## 2. `ExecutionContext`

`ExecutionContext.data` 主要包含：

- `initial`
- `inputs`
- `nodes`
- `loop`
- `cid`
- `task_services`

模板默认可见：

- `state`
- `initial`
- `inputs`
- `loop`
- `nodes`

## 3. 节点结果结构

一个 step 结束后，运行时会把结果写进 `nodes.<step_id>`。

典型字段：

- `output`
- 自定义输出键
- `run_state`
- `metadata`

`run_state` 典型内容：

- `status`
- `start_time`
- `end_time`
- `duration`
- `error`

## 4. Action 调用与注入

运行时通过 `ActionInjector` 调 action。

行为要点：

- 先解析 action 名
- 再对 `params` 做模板渲染
- 再进行参数注入
- 同步函数会切到 executor 执行
- 异步函数直接 `await`

可自动注入：

- `context`
- `engine`
- `@requires_services` 声明的服务依赖

## 5. 依赖与调度

`ExecutionEngine` 会先构建图，再调度 ready 节点。

关键语义：

- 普通字符串依赖：依赖某个 step 完成
- 列表依赖：隐式 `all`
- 逻辑依赖：`all / any / none`
- 状态依赖：读取上游 step 的 `run_state.status`
- 循环依赖会在构图阶段直接报错

## 6. `when`、`step_note` 与循环

### `step_note`

- 节点启动前渲染
- 作用域只包含 `inputs`、`loop` 和已渲染 `params`
- 适合输出人类可读的步骤说明

### `when`

- 在依赖满足后执行
- 使用完整模板作用域
- 渲染失败时会报错
- 布尔值为 false 时节点直接 `SKIPPED`

### 循环

- `for_each`
  每个 item fork 一个子上下文
- `times`
  按 index 重复执行
- `while`
  每次循环重新评估条件

## 7. 重试与超时

### 异常重试

- 由 `on_exception` 或兼容 `retry / retry_on / retry_delay` 驱动
- 仅当异常类型命中 `retry_on` 时才继续

### 结果重试

- 由 `on_result.retry_when` 或兼容 `retry_condition` 驱动
- 条件基于 `result` 和 `attempt`

### 超时

- 节点级超时由 `timeout` / `timeout_sec` 控制
- 未配置时可回退到 engine 默认值

## 8. 任务返回值

task 最终返回一个对象：

- `status`
  `SUCCESS` / `FAILED` / `ERROR`
- `user_data`
  `returns` 的渲染结果；若未配置默认是 `true`
- `framework_data`
  完整 `ExecutionContext.data`
- `error`
  任务级错误信息

## 9. 与调度器的关系

调度器层面还会追加这些语义：

- task 进入 queue 时发布 `queue.enqueued`
- dequeue 时发布 `queue.dequeued`
- 执行结束时发布 `queue.completed`
- `ExecutionManager` 会处理并发信号量、资源标签和状态规划

## 10. 常见理解误区

- `outputs` 不会替代 `run_state`
- `returns` 不等于 `framework_data`
- `step_note` 不是控制流字段
- `when` 不会回写输入
