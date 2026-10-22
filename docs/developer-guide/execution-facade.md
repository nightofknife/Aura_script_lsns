# Core 执行 facade

第三批将 Python 业务操作中的执行装配收敛到 `ExecutionFacade`。公开入口由 `ExecutionEngine.bind_execution(context)` 提供，handle 绑定当前调用的 engine 和 ExecutionContext，不注册为服务单例，也不放进 `context.data` 或跨任务缓存。

## Typed action 与 YAML 参数

```python
execution = engine.bind_execution(context)
snapshot = await execution.call_action(
    "resonance_pc.player_data_refresh",
    {"stages": ["profile"], "profile_sections": ["fatigue"]},
)
```

`call_action(action_ref, values=None, *, scope=None)` 接受已经求值的 Python 参数。字符串即使含有 `{{ ... }}` 也按原值传递，不再读取 state scope 或进行模板渲染。action 引用仍走既有 `ActionResolver`，沿用包依赖限制、middleware、参数准备和服务注入。

注入顺序仍是 `task_services[FQID]`、engine services、service resolver。业务 values 不得覆盖服务、engine 或 context 的注入。同步 action 使用公共受追踪 worker 和取消收尾机制；异步 action 保留 activity/middleware 执行链。

YAML 节点仍通过 `ActionInjector.execute` 先完成一次模板渲染，再进入同一个 typed 执行核。Python 调用不通过伪造模板或 identity renderer 避免渲染，也不直接调用 action 函数跳过注入链。

Facade 只表示一次调用，不等价于 NodeExecutor 节点。节点的 when、loop、retry、输出映射和节点超时仍由任务定义与 NodeExecutor 管理。Facade 不自动重试输入或消费操作。

## 完整子任务

```python
investment = await execution.run_task(
    "tasks:trade_goods_investment_pc.yaml:trade_goods_investment_pc",
    {"mode": mode, "city_name": city_name},
)
```

`run_task(task_ref, inputs=None, *, scope=None, result_assertions=None)` 使用 canonical `tasks:<path>.yaml[:task_key]`，复用 `TaskRefResolver` 和当前包限制。跨包、绝对路径、路径穿越和旧 `task_name` 入口不被放宽。

执行进入现有 `orchestrator.execute_task`：子任务完整 DAG、returns、节点收尾、父子 CID 和 task.finished 生命周期均保留。它不跳到私有子节点，也不读取 framework_data 中某个节点的 output 冒充业务结果。YAML 的 `aura.run_task` 同样委托这个执行核。

返回仍是子任务声明的 `user_data`。保持既有规则：拒绝 None 和空 dict；显式 `returns: true` 合法，不在本次迁移中额外改变其他结果类型的兼容性。

可选 `result_assertions` 使用非空 dotted path，并要求实际值与期望值类型相同且值相等，因此 True 不能等价于整数 1。业务特定的 status 集合、页面身份与结果分类仍由领域决定。

失败携带既有 failure_evidence；原生 CancelledError 继续传播。显式 scope 或当前 operation 的停止状态/截止在调用前后检查，迟到结果不得授权后续流程；取消子任务时等待既有任务收尾，不遗留脱离调用者的后台任务。阻塞同步工作仍遵循已有 drain/隔离机制，截止不承诺硬实时停止。

显式 scope 必须共享当前 operation 的停止状态，不能延长截止或替换已绑定的目标。等待期间每隔至多 50ms 检查停止与截止；重复取消调用者时继续等待子任务 finally 收尾。子任务使用自身 CID 记录调用，同时继承父级停止状态。

## 本批 city_trade 迁移

| 原入口 | 新执行边界 | 继续保持的领域规则 |
| --- | --- | --- |
| `_run_trade_goods_investment_task` | `bind_execution(context).run_task(...)` | 投资完整任务；success is True、shop_page、invested/skipped、mode 与 target_level 严格 int；进度回调 finally 复位 |
| `_call_recovery_action` | `bind_execution(context).call_action(...)` | 基础导航/疲劳刷新 action 的 dict 返回与页面/新鲜度检查 |
| `_run_black_moon_local_purchase_task` | 显式 `run_task(...)` | 完整采购任务、selection/week/city/status 和 rest_menu 返回确认 |

上述入口不再 import 或手工装配 `ActionInjector` / `TemplateRenderer`。恢复 action 适配器明确拒绝把 `aura.run_task` 混作普通 action，子任务调用在代码中直接表达。

投资的业务结果检查继续在领域层进行，以保留 `goods_investment_task_incomplete` 与原 result 详情；不在 facade 前置重复断言，把该错误改成通用 task failed。原框架失败仍为 `goods_investment_task_failed`，包装补充 cause 和 evidence。

气泡水次数/疲劳预算、共享休息点 session、消费提交与回程策略未改变。便当仍由 `auto_cycle_trade_pc.yaml` 的独立完整子任务执行，然后交给 `finish_auto_cycle_trade` 合并；2000 恢复目标、不可超目标及组合商贸拒绝 auto_bento 的限制保持。

本批只移除指定的手工执行装配。海角投资及其他领域 action 互调、商贸领域操作提取属于后续批次，不借本次迁移扩大范围。现有 context/engine 参数保留；本批没有新增名为 execution 的自动注入参数或业务 schema。

## 实施与验证状态

本批完成源码实施与源码审阅，未新增测试代码，也未运行测试、识别差分、编译检查、性能基准或打包。前两批的通过结果对应此前版本，不能作为本批验证结论。

后续获授权验证时，重点覆盖 typed 字符串与 YAML 单次渲染、task-service 注入优先级、严格类型返回断言、子任务完整收尾/父子取消、原 evidence、投资未完成错误码及恢复返回主屏后的状态刷新。
