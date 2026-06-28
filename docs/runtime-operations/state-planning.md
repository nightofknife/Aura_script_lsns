# 状态规划

Aura 当前支持在任务执行前自动做状态规划，确保系统先进入任务要求的初始状态。

## 1. 何时触发

当任务的 `meta.requires_initial_state` 被设置时，`ExecutionManager` 会在真正提交任务前触发状态规划。

前提：

- 当前 plan 配置了 `states_map.yaml`
- `PlanManager` 已为该 plan 创建 `StatePlanner`

## 2. `states_map.yaml` 结构

核心结构：

```yaml
states:
  idle:
    check_task: "tasks:checks:idle.yaml"
    can_async: true
    priority: 10

  ready:
    check_task: "tasks:checks:ready.yaml"
    can_async: false
    priority: 20

transitions:
  - from: idle
    to: ready
    cost: 1
    transition_task: "tasks:transitions:to_ready.yaml"
```

## 3. 规划流程

`StatePlanner` 会做三件事：

1. `determine_current_state(target_state)`
2. `find_path(start, target)`
3. `verify_state_with_retry(state_name)`

## 4. `determine_current_state`

行为：

- 优先检查距离目标状态更近的 state
- 可异步的检查任务会并行执行
- 不可异步的检查任务串行执行
- 一旦有 state 检查返回成功且 `user_data` 为真，就认定当前状态

## 5. `find_path`

基于 transition graph 做最小成本路径规划。

输出是一串 `transition_task`。

## 6. 失败保护

当前有两层保护：

- `max_depth`
- `max_replans`

同时：

- `source == state_planning` 的任务会跳过再次规划，避免无限递归

## 7. 示例：需要初始状态的任务

任务：

```yaml
meta:
  title: "开始战斗"
  requires_initial_state: ready
steps:
  start:
    action: log
    params:
      message: "battle"
```

状态图：

```yaml
states:
  idle:
    check_task: "tasks:checks:idle.yaml"
  ready:
    check_task: "tasks:checks:ready.yaml"

transitions:
  - from: idle
    to: ready
    cost: 1
    transition_task: "tasks:transitions:prepare_battle.yaml"
```

## 8. 何时用 `PlanContext.state`

- 跨任务、跨重启仍需保留的数据，用 `PlanContext.state`
- 只和本次执行有关的中间结果，用 `ExecutionContext.nodes`

## 9. 常见问题

- `requires_initial_state` 配了，但 plan 没有 `states_map.yaml`
- `check_task` 返回不稳定
- transition task 自己又触发状态规划
