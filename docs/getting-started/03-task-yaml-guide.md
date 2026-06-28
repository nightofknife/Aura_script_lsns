# 任务 YAML 指南

> `docs/schemas/task-schema.json` 是任务 DSL 的静态校验契约。
> 运行时仍兼容少量额外写法，例如单任务文件的根级 `meta/steps/returns` 结构。
> 本文档以当前运行时代码行为为准。

## 1. 文件组织

任务文件默认位于 plan 的 `tasks/` 目录，或 `manifest.yaml` 的 `task_paths` 中声明的目录。

```text
plans/MyPlan/
|-- manifest.yaml
`-- tasks/
    |-- login.yaml
    `-- combat/
        `-- farm.yaml
```

支持两种文件结构。

### 单任务文件

```yaml
meta:
  title: 登录
steps:
  open_page:
    action: log
    params:
      message: "open"
```

### 多任务文件

```yaml
login:
  meta:
    title: 登录
  steps:
    s1:
      action: log

logout:
  meta:
    title: 登出
  steps:
    s1:
      action: log
```

## 2. `task_ref`

统一使用 canonical `task_ref`：

- `tasks:<path>.yaml`
- `tasks:<path>.yaml:<task_key>`

示例：

- `tasks:login.yaml`
- `tasks:combat:farm.yaml`
- `tasks:auth.yaml:logout`

不支持：

- `tasks/auth/login`
- `task_name`
- 跨包 task 调用

## 3. 顶层结构

### 单任务 root 结构

```yaml
meta: {}
steps: {}
returns: {}
```

### 多任务 task map 结构

```yaml
my_task:
  meta: {}
  steps: {}
  returns: {}
```

## 4. `meta`

示例：

```yaml
meta:
  title: "任务标题"
  description: "任务说明"
  entry_point: true
  concurrency: exclusive
  inputs:
    - name: username
      type: string
      required: true
```

字段：

- `title`
  UI 和日志展示名
- `description`
  描述信息
- `entry_point`
  TUI 会把它当作可执行入口任务候选
- `concurrency`
  并发控制配置
- `inputs`
  任务输入 schema

### `concurrency`

支持：

- `exclusive`
- `concurrent`
- `shared`

对象形式：

```yaml
meta:
  concurrency:
    mode: shared
    resources:
      - mouse
      - api:openai:5
      - file:data.json:1
    mutex_group: ui_automation
    max_instances: 2
```

当前实现语义：

- `exclusive`
  附加全局互斥资源标签
- `concurrent`
  不附加互斥资源标签
- `shared`
  使用 `resources`，并可附加 `mutex_group` 和 `max_instances`
- `resources`
  基本按字符串透传，执行层只把最后一个 `:N` 识别为并发上限

## 5. `meta.inputs`

示例：

```yaml
meta:
  inputs:
    - name: mode
      type: string
      enum: ["easy", "hard"]
      default: "easy"

    - name: retry_count
      type: number
      min: 0
      max: 10
      default: 3

    - name: tags
      type: list<string>
      count: "1-5"

    - name: profile
      type: dict
      properties:
        uid:
          type: string
          required: true
        debug:
          type: boolean
          default: false
```

支持类型：

- `string`
- `number`
- `boolean`
- `list`
- `dict`
- `list<type>`

运行时语义：

- 多余输入会报错
- `dict` 默认不允许额外字段
- `options` 会归一化为 `enum`
- `count` 支持 `3`、`<=5`、`>=2`、`1-3`、`[1,3]`
- 不支持 `integer` / `array` / `object`
- `type` 会参与实际校验与规范化，不只是 UI 提示

## 6. `steps`

```yaml
steps:
  step_id:
    action: log
    params:
      message: "hello"
```

常用字段：

- `action`
- `params`
- `outputs`
- `depends_on`
- `when`
- `loop`
- `retry`
- `on_exception`
- `on_result`
- `retry_delay`
- `retry_on`
- `retry_condition`
- `timeout`
- `timeout_sec`
- `step_note`

已移除字段：

- `label`
- `goto`

## 7. `depends_on`

### 基本依赖

```yaml
depends_on: fetch_data
```

### 逻辑组合

```yaml
depends_on:
  all: [a, b]
```

```yaml
depends_on:
  any: [a, b]
```

```yaml
depends_on:
  none: [a, b]
```

### 状态依赖

```yaml
depends_on:
  fetch_data: "success|failed"
```

支持状态：

- `success`
- `failed`
- `running`
- `skipped`

不支持：

- `depends_on: [a, b, c]`
- `and` / `or` / `not`
- 在 `depends_on` 里内联 `when:...`

## 8. `when`

```yaml
steps:
  run_if_needed:
    action: log
    when: "{{ inputs.enabled }}"
    params:
      message: "enabled"
```

当前行为：

- `when` 必须是字符串
- 先用 template renderer 渲染
- 再按布尔语义转换
- 为 `false` 时 step 标记为 `SKIPPED`

## 9. `loop`

### `for_each`

```yaml
loop:
  for_each: "{{ inputs.items }}"
  parallelism: 4
```

### `times`

```yaml
loop:
  times: 3
  parallelism: 2
```

### `while`

```yaml
loop:
  while: "{{ nodes.check.output }}"
  max_iterations: 100
```

说明：

- `loop.item`、`loop.index` 会写入 `ExecutionContext`
- 未配置 `max_iterations` 时默认 `1000`

## 10. 重试与超时

### 推荐写法

```yaml
on_exception:
  retry: 3
  retry_on: ["TimeoutError"]
  delay: 1

on_result:
  retry_when: "{{ result.status != 200 }}"
  max_retries: 2
  delay: 1
```

### 兼容写法

```yaml
retry: 3
retry_delay: 1
retry_on: ["TimeoutError"]
retry_condition: "{{ result.status != 200 }}"
```

或：

```yaml
retry:
  count: 3
  delay: 1
  retry_on: ["TimeoutError"]
  condition: "{{ result.status != 200 }}"
```

### 超时

```yaml
timeout: 30
```

或：

```yaml
timeout_sec: 30
```

注意：

- 当前实现不支持 `retry.interval`
- 当前实现不会把 `on_exception: null` 解释为“重试所有异常”

## 11. 输出、返回值与上下文

### `outputs`

```yaml
steps:
  query:
    action: http.get
    params:
      url: "https://example.com"
    outputs:
      code: "{{ result.status }}"
```

### `returns`

```yaml
returns:
  ok: "{{ nodes.query.run_state.status == 'SUCCESS' }}"
  status_code: "{{ nodes.query.code }}"
```

运行时关系：

- 配置 `outputs`
  step 结果按模板写入 `nodes.<step>.*`
- 未配置 `outputs`
  action 返回值写入 `nodes.<step>.output`
- 配置 `returns`
  渲染结果作为 task 的 `user_data`
- 未配置 `returns`
  `user_data` 默认是 `true`
- `framework_data`
  是整个 `ExecutionContext.data`

## 12. 运行语义补充

### Action 解析

- 未带 `/` 的 action 名先尝试解析为当前 package 内 action
- 当前 package 未导出该 action 时立即报错
- 显式外部 action 必须已在 manifest 依赖中声明

### `aura.run_task`

```yaml
steps:
  call_sub_task:
    action: aura.run_task
    params:
      task_ref: "tasks:combat:farm.yaml"
      inputs:
        times: 3
```

约束：

- 必须使用 `task_ref`
- `inputs` 必须是对象
- 只允许当前 plan 内任务

### `ExecutionContext`

模板作用域默认包含：

- `state`
- `initial`
- `inputs`
- `loop`
- `nodes`

其中：

- `inputs`
  是任务输入
- `loop`
  是当前循环变量
- `nodes`
  是已执行节点结果

### `step_note` 与 `when`

- `step_note`
  只使用 `inputs`、`loop`、已渲染的 `params`
- `when`
  使用完整渲染作用域

### 失败、跳过和重试

- `when` 为 false 时，step 进入 `SKIPPED`
- 节点异常时，`run_state.status` 会变为 `FAILED`
- 配置重试时，只有满足异常匹配或结果条件时才继续尝试
- 超时通过 `asyncio.wait_for()` 处理

## 13. 语义示例

```yaml
meta:
  title: "示例任务"
  inputs:
    - name: urls
      type: list<string>
      required: true

steps:
  prepare:
    action: log
    params:
      message: "prepare"
    step_note: "准备 {{ params.message }}"

  fetch_each:
    action: http.get
    depends_on: prepare
    loop:
      for_each: "{{ inputs.urls }}"
      parallelism: 2
    params:
      url: "{{ loop.item }}"
    on_exception:
      retry: 2
      retry_on: ["TimeoutError"]
      delay: 1
    outputs:
      raw: "{{ result }}"

  finish:
    action: log
    depends_on:
      fetch_each: "success|failed"
    when: "{{ nodes.prepare.run_state.status == 'SUCCESS' }}"
    params:
      message: "done"

returns:
  finished: "{{ nodes.finish.run_state.status == 'SUCCESS' }}"
```

## 14. 常见错误

- `task_ref` 使用斜杠路径：改成 `tasks:...yaml[:task_key]`
- `task_ref` 缺少 `.yaml`：补上后缀
- `aura.run_task` 仍传 `task_name`：改成 `task_ref`
- `depends_on` 使用列表简写：改成 `{ all: [...] }`
- `depends_on` 使用 `and/or/not`：改成 `all/any/none`
- `depends_on` 内写 `when:`：改成 step 级 `when`
- 使用 `goto/label`：改成 `step_note` + 依赖/条件建模

## 15. 下一步

- 执行语义细节：见 [运行时行为](./04-runtime-behavior.md)
- package 依赖与 `task_ref`：见 [任务引用与依赖](../package-development/task-references-and-dependencies.md)
