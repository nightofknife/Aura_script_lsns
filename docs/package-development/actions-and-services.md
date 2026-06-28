# Action 与 Service 开发

Aura 当前通过装饰器和 registry 暴露 action 与 service。

## 1. Action 装饰器

使用 `@action_info` 给函数附加元数据：

```python
from packages.aura_core.api.decorators import action_info, requires_services


@action_info(name="ping", public=True, read_only=True, description="Simple action")
@requires_services(logger="core/event_bus")
async def ping(message: str, logger):
    return {"message": message}
```

`@action_info` 常用参数：

- `name`
- `read_only`
- `public`
- `description`
- `visibility`
- `timeout`

运行时会提取：

- 参数列表
- 是否 async
- 描述
- 源文件信息

## 2. `@requires_services`

用于声明 action 的服务依赖。

两种写法：

```python
@requires_services("core/config")
```

```python
@requires_services(config="core/config", state_store="core/state_store")
```

规则：

- 位置参数默认别名取 service id 的最后一段
- 别名冲突会报错
- 注入在 action 调用阶段完成

## 3. Service 装饰器

使用 `@service_info` 给类附加元数据：

```python
from packages.aura_core.api.decorators import service_info


@service_info(
    alias="example_service",
    public=True,
    singleton=True,
    deps={"config": "core/config"},
)
class ExampleService:
    def __init__(self, config):
        self.config = config
```

常用参数：

- `alias`
- `public`
- `description`
- `visibility`
- `singleton`
- `config_schema`
- `replace`
- `deps`

## 4. 注册与 FQID 规则

### Action

- action FQID 来源于 package canonical id
- 标准格式：`author/package/action`
- 同 package 内同名 action 不允许重复注册
- 简名索引仍保留兼容，但跨包重名时会被后注册覆盖

### Service

- service FQID 形如 `package_id/alias`
- core service 形如 `core/<alias>`
- registry 同时维护 active alias 和 fqid map

## 5. Service replace 规则

replace 策略是严格模式：

- 不允许无声明覆盖已有 alias
- 只有显式 `replace` 才允许替换非 core service
- core service 不能被替换
- replace 目标必须存在、唯一、并且契约兼容

## 6. Action 调用时的参数注入

调用 action 时，运行时会按以下顺序补齐参数：

- Pydantic model 参数
- service 依赖
- `context`
- `engine`
- 渲染后的普通参数
- `**kwargs`

这意味着：

- 参数名与注入别名冲突时，以注入规则为准
- service 依赖不需要写进 `params`

## 7. 最小 package 示例

```python
from packages.aura_core.api.decorators import action_info, service_info, requires_services


@service_info(alias="greeting_service", public=True, singleton=True)
class GreetingService:
    def greet(self, name: str) -> str:
        return f"hello, {name}"


@action_info(name="greet_user", public=True, read_only=True)
@requires_services(greeting_service="example/greeting_service")
def greet_user(name: str, greeting_service: GreetingService):
    return {"message": greeting_service.greet(name)}
```

## 8. Package + Plan 联动示例

假设 package 导出了 `Aura/example/greet_user`：

```yaml
meta:
  title: "调用 package action"
steps:
  s1:
    action: Aura/example/greet_user
    params:
      name: "Aura"
returns:
  message: "{{ nodes.s1.output.message }}"
```

前提是当前 plan 的 manifest 已声明对 `@Aura/example` 的依赖。

## 9. 常见错误

- action 简名解析到外部包，但 manifest 未声明依赖
- service alias 冲突
- replace 目标不存在
- 替换 core service
- service 构造依赖链循环
