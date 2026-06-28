# 任务引用与依赖

本页说明 task reference、action dependency 和 service dependency 的运行时规则。

## 1. canonical `task_ref`

当前只支持：

- `tasks:<dir>:<file>.yaml`
- `tasks:<dir>:<file>.yaml:<task_key>`

示例：

- `tasks:login.yaml`
- `tasks:combat:farm.yaml`
- `tasks:auth.yaml:logout`

不支持：

- `tasks/auth/login`
- 绝对路径
- `..`

## 2. `TaskReference` 与 `TaskRefResolver`

解析过程会产出：

- `task_ref`
- `task_file_path`
- `task_key`
- `loader_path`
- `canonical_task_id`

约束：

- 必须提供 `default_package`
- 当前 canonical 模式下不支持跨 package task 调用
- `enforce_package` 不匹配时会直接报错

## 3. `aura.run_task`

`aura.run_task` 内部也是走 `TaskRefResolver.resolve()`。

因此：

- 子任务仍然必须是 canonical `.yaml` 格式
- 只允许当前 plan 内部 task
- `inputs` 必须是对象

## 4. Action 依赖

未带 `/` 的 action 名解析规则：

1. 优先解析为当前 package 的本地 action
2. 当前 package 未导出该 action 时立即报错
3. 外部 action 必须显式写完整 `author/package/action`
4. 显式外部 action 的目标 package 必须已声明依赖

显式外部 action：

```yaml
action: Aura/example/greet_user
```

格式必须是：

```text
author/package/action
```

## 5. Service 依赖

action 和 service 都可以声明 service 依赖。

### action service 依赖

通过 `@requires_services` 声明：

- 本地 service 可以写本地 alias
- 外部 service 要写完整 `package/service`
- `core/...` 可以直接引用

### service deps

通过 `@service_info(..., deps={...})` 声明。

规则与 action 类似：

- 本地 service alias 会解析成 `package_id/alias`
- 引用外部 service 时，目标 package 必须在 manifest 依赖中声明
- 非 public 的外部 service 不能被消费

## 6. 常见错误

- `task_ref` 少了 `.yaml`
- `task_ref` 使用 slash
- 裸 action 名未在当前 package 导出
- 外部 action 未声明依赖
- service dependency 指向不存在的 local alias
- 消费 private service
