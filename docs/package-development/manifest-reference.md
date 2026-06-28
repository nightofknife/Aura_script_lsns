# Manifest 参考

Aura 的 package / plan 装载依赖 `manifest.yaml`。`PackageManager` 会根据 manifest 发现包、校验依赖、决定加载顺序并注册导出能力。

## 1. 当前 manifest 模型

核心结构来自 `packages/aura_core/packaging/manifest/schema.py`。

顶层主要字段：

- `package`
- `requires`
- `dependencies`
- `pypi_dependencies`
- `exports`
- `extends`
- `overrides`
- `lifecycle`
- `configuration`
- `resources`
- `build`
- `trust`
- `task_config`
- `metadata`

## 2. 最小示例

```yaml
package:
  name: "@Aura/example"
  version: "0.1.0"
  description: "Example package"
  license: "MIT"

exports:
  services: []
  actions: []
  tasks: []
```

## 3. `package`

常见字段：

- `name`
- `version`
- `description`
- `license`
- `authors`
- `homepage`
- `repository`
- `keywords`
- `categories`

注意：

- `canonical_id` 在运行时等于 `name.lstrip("@")`
- action FQID 会依赖这个 canonical id 生成

## 4. `requires`

用于描述对 Aura 版本的要求：

```yaml
requires:
  aura: ">=0.1.0"
```

## 5. `dependencies`

支持 local / git 依赖，字段来自 `DependencySpec`：

- `name`
- `version`
- `source`
- `path`
- `git_url`
- `git_ref`
- `optional`
- `features`

## 6. `exports`

包含三类导出：

- `services`
- `actions`
- `tasks`

### `exports.services`

字段：

- `name`
- `module`
- `class_name`
- `public`
- `singleton`
- `description`
- `replace`

### `exports.actions`

字段：

- `name`
- `module`
- `function_name`
- `public`
- `read_only`
- `description`
- `timeout`
- `parameters`

### `exports.tasks`

当前主要是元数据导出，运行时 task 索引仍以 `task_paths` + `TaskLoader` 为准。

## 7. `lifecycle`

支持：

- `on_install`
- `on_uninstall`
- `on_load`
- `on_unload`

值的格式是 `module:function`。

## 8. `configuration`

字段：

- `default_config`
- `config_schema`
- `user_template`
- `allow_user_override`
- `merge_strategy`

## 9. `resources`

资源映射：

- `templates`
- `data`
- `assets`

`PluginManifest` 提供：

- `get_template_path()`
- `get_data_path()`
- `get_asset_path()`

## 10. `task_config`

当前最关键字段：

- `task_paths`

示例：

```yaml
task_config:
  task_paths:
    - tasks
    - extra_tasks
```

安全限制：

- 不能为空
- 不能包含 `..`
- 不能是绝对路径

## 11. 加载顺序与模式

`PackageManager` 行为：

- 发现 `packages/` 和 `plans/` 下的候选目录
- 在 `strict / hybrid / off` 三种 manifest mode 下运行
- 可自动 sync manifest
- 按依赖图做拓扑排序
- hybrid mode 下，manifest 缺失或解析失败时可生成 fallback manifest

## 12. 常见错误

- 缺失必需依赖
- 依赖图成环
- `exports.services` 或 `exports.actions` 指向的 module/class/function 不存在
- service replace 目标不存在或不兼容
- `task_paths` 非法
