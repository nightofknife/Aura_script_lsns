# 框架打包与外置 Plans 方案

## 目标

希望把 Aura 框架本身打包成可分发的运行时，但 `plans/` 仍然保持为普通文件夹，方便：

- 手动修改 YAML 任务
- 手动修改 plan 内的 Python action/service
- 按目录增删 plan
- 在不覆盖用户 plan 的前提下升级框架运行时

这份方案基于当前仓库代码行为设计，尽量优先走“少改代码即可落地”的路径。

## 现状约束

当前代码已经具备“框架与工作区分离”的基础能力，但它对目录结构有几个明确假设：

1. 运行时根目录来自 `AURA_BASE_PATH`，否则默认取当前项目根目录；冻结后默认取可执行文件所在目录。
2. `PlanManager` 固定从 `<base_path>/plans` 加载 plan，从 `<base_path>/packages` 加载包。
3. `PackageManager` 通过扫描真实文件系统中的 `manifest.yaml` 或 `game.yaml` 来发现 plan，不是从内嵌资源清单里读。
4. plan 内 Python 模块是通过 `importlib` 动态导入的，默认导入路径是 `plans.<plan_name>...`。
5. 热重载逻辑默认也把 `<base_path>/plans` 视为可编辑源码目录。

这意味着：

- `plans/` 非常适合做成外置源码目录
- 不适合把可编辑 plan 封进单文件只读包里
- 最自然的交付形态是“运行时包 + 外部工作区”

> 注意
>
> 上面这个判断适用于“可以调整发布结构，必要时也可以配合少量框架改造”的总体方案。
> 如果约束进一步收紧为“**完全不修改框架代码**”，则当前仓库里存在硬编码相对路径
> 例如 `.\plans\aura_base\...` 的 OCR 模型路径，发布时应把 `plans/` 放在发布根目录，
> 而不是 `workspace/plans/` 这种下一层目录。

## 推荐方案

推荐使用“双层发布”：

- 第一层：`runtime/`
  只放打包后的框架运行时，可整体替换升级
- 第二层：`workspace/`
  只放用户可编辑内容，比如 `plans/`、`config.yaml`、`.env`、`logs/`

启动时通过 `AURA_BASE_PATH` 指向 `workspace/`，让框架把这个目录视为运行根目录。

### 推荐目录结构

```text
release/
  runtime/
    aura.exe
    _internal/...
  workspace/
    config.yaml
    .env
    logs/
    plans/
      __init__.py
      aura_base/
        __init__.py
        manifest.yaml
        tasks/
        src/
      resonance/
        __init__.py
        manifest.yaml
        tasks/
        src/
  run.ps1
```

其中：

- `runtime/` 可以整体替换，不碰用户工作区
- `workspace/plans/` 保留源码形态，允许直接编辑
- `workspace/config.yaml` 放运行时配置
- `workspace/logs/` 放输出日志，避免写进只读运行时目录

## 为什么推荐 `onedir`，不推荐 `onefile`

优先推荐 `PyInstaller onedir` 这类目录式分发，不建议一开始就追求单文件。

原因有三个：

1. 当前框架大量依赖真实目录结构
   `PackageManager`、`TaskLoader`、热重载、日志目录都更适合稳定的文件系统路径。
2. 依赖较重
   当前运行时依赖包含 `pywin32`、`dxcam`、`opencv-python`、`av`、`paddleocr`、`paddlepaddle-gpu`，单文件模式通常会带来更慢启动、更复杂的动态库提取和更高的不确定性。
3. 外置 plan 的需求本身就意味着“真实文件夹”
   既然 `plans/` 要外置可编辑，目录式分发的收益更高，也更符合当前架构。

如果后续一定要尝试 `onefile`，也应建立在 `onedir` 已稳定之后，再单独验证。

## 方案细节

### 方案 A：推荐，运行时与工作区分离

结构：

```text
release/
  runtime/
  workspace/
  run.ps1
```

启动脚本只做两件事：

1. 设置 `AURA_BASE_PATH=<release>/workspace`
2. 调用 `<release>/runtime/aura.exe`

优点：

- 升级框架时只替换 `runtime/`
- 用户改过的 `plans/` 不会被覆盖
- 多个工作区可以共用一套运行时
- 目录职责清晰，便于后续做“官方运行时 + 项目工作区”

缺点：

- 需要一个启动脚本或快捷方式来注入 `AURA_BASE_PATH`

### 方案 B：次优，运行时与 plans 同级

结构：

```text
release/
  aura.exe
  plans/
  config.yaml
  logs/
```

这也是当前代码最省事的方案，因为冻结后默认就把可执行文件所在目录当作 `base_path`。

优点：

- 不需要额外设置环境变量
- 发布结构最简单

缺点：

- 升级运行时时，容易和用户修改过的 `plans/` 混在一起
- 二进制和用户内容耦合较强

如果你只是做单机自用版本，可以先用方案 B；如果要长期维护或给别人用，建议直接上方案 A。

## 构建建议

### 第一步：保留工作区源码结构

工作区里至少保留这些内容：

- `plans/__init__.py`
- 每个 plan 目录下的 `__init__.py`
- `manifest.yaml`
- `tasks/`
- `src/`

这很重要，因为当前 plan Python 模块是按 `plans.<plan_name>...` 动态导入的。

### 第二步：把框架打包成运行时

建议使用独立构建虚拟环境，把 `cli.py` 作为入口打包为目录式可执行包。

建议的打包原则：

- 打包入口：`cli.py`
- 打包模式：`onedir`
- 框架代码打进运行时
- plan 源码不要打进运行时
- 运行时依赖全部跟随 `runtime/` 分发

这里更适合使用 `.spec` 文件维护隐藏导入、数据文件和原生库，而不是只靠一条命令拼参数。

### 第三步：组装发布目录

构建完成后，再额外组装一份工作区：

```text
workspace/
  config.yaml
  .env
  plans/
```

这一步建议做成构建脚本中的“assemble”阶段，而不是手工拷贝。

### 第四步：提供启动脚本

PowerShell 启动脚本的职责应该是：

```powershell
$env:AURA_BASE_PATH = "<workspace absolute path>"
& "<runtime absolute path>\\aura.exe" @Args
```

这样运行时和工作区就彻底解耦了。

## 对 plan 手工修改的支持策略

### 修改 YAML 任务

这是最适合外置化的内容，直接编辑 `plans/<plan>/tasks/*.yaml` 即可。

### 修改 plan 内 Python 代码

也是可行的，但要注意：

- 新增 action/service 后，当前运行时仍然依赖 `manifest.yaml` 导出
- 只改已有函数逻辑通常没问题
- 新增运行时符号时，需要同步更新 `manifest.yaml`

如果希望“用户加了新 action/service 后尽量少踩坑”，可以在工作区 `config.yaml` 中显式开启：

```yaml
package:
  manifest_mode: hybrid
  auto_sync_manifest_on_startup: true
```

这样启动时会尝试自动同步 manifest，降低手工维护成本。

### 热重载

当前热重载路径默认围绕 `<base_path>/plans` 工作，因此外置工作区与当前实现是匹配的。

前提是：

- 工作区目录真实存在于文件系统
- 运行时包含文件监控依赖

如果发布包里不带 `watchdog`，那就建议用户修改后重启运行时，而不是依赖热重载。

## 推荐的发布流程

建议把发布拆成两个产物：

1. `runtime.zip`
   只包含打包后的运行时
2. `workspace-template.zip`
   只包含初始 `plans/`、`config.yaml`、`.env` 模板

落地时可以这样升级：

- 升级框架：只替换 `runtime/`
- 升级官方 plan 模板：只合并 `workspace-template`
- 用户自定义 plan：保留在自己的 `workspace/plans/` 下

这比把所有内容混在一个目录里更利于长期维护。

## 当前代码下的最小实现路径

如果尽量不改现有代码，推荐顺序如下：

1. 先采用 `PyInstaller onedir` 打包 `cli.py`
2. 使用方案 A 组织发布目录：`runtime/ + workspace/`
3. 通过启动脚本设置 `AURA_BASE_PATH`
4. 把 `plans/` 作为源码目录原样放进 `workspace/`
5. 在工作区 `config.yaml` 中按需打开 `auto_sync_manifest_on_startup`

这条路径和当前代码假设最一致，改造最少，风险最低。

## 当前仓库里的落地入口

当前仓库已经补充了零改框架代码的打包资产：

- `packaging/pyinstaller/aura.spec`
  `PyInstaller onedir` 运行时 spec
- `packaging/templates/run.ps1`
  发布包入口脚本模板
- `packaging/templates/config.yaml`
  发布态默认配置模板
- `scripts/build_release.ps1`
  一键构建并组装发布目录的脚本

默认输出目录位于：

```text
.runtime/release/aura-release/
```

这个发布目录采用的是“`plans/` 位于发布根目录”的零改代码布局，而不是 `workspace/plans/` 布局。

## 后续可选增强

如果你后面想把这件事做得更产品化，建议再补三个增强点：

1. 增加 `--workspace` CLI 参数
   这样不用依赖环境变量，启动体验更直观。
2. 支持多个 plan 搜索目录
   例如 `plan_search_paths: ["./plans", "D:/shared_plans"]`。
3. 增加“发布组装脚本”
   自动完成 runtime 打包、workspace 模板复制、启动脚本生成。

## 结论

结论很明确：

- 可以打包框架，同时把 `plans/` 保持为独立可编辑文件夹
- 最适合当前项目的方式是“目录式运行时 + 外置工作区”
- 最推荐的落地形态是 `runtime/ + workspace/ + 启动脚本`
- 不建议把可编辑 plan 和重依赖运行时一起塞进单文件包

如果后续要真正落地实施，下一步最值得做的是：

- 增加一个发布组装脚本
- 或者直接补一个 `PyInstaller` 的 `.spec` 和 `run.ps1` 模板
