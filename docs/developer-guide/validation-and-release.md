# Validation and Release

## Plan Validation

```powershell
python -m packages.aura_core.cli.package_cli check plans/resonance
python -m packages.aura_core.cli.package_cli validate plans/resonance
python tools\plan_doctor.py --plan resonance

python -m packages.aura_core.cli.package_cli check plans/resonance_pc
python -m packages.aura_core.cli.package_cli validate plans/resonance_pc
python tools\plan_doctor.py --plan resonance_pc
```

## Project Smoke Tests

所有测试、验证、冒烟检查和打包检查都必须从仓库根目录运行，测试产生的临时文件、
缓存、截图、日志和其他产物也必须保存在当前仓库内。禁止把系统临时目录、桌面、
其他 checkout 或外部项目目录作为测试工作目录。

`pytest.ini` 将默认临时目录设为 `.pytest_tmp`。显式指定 `tests/smoke`
可以单独运行源码冒烟，覆盖项目导入、计划发现、Runner、GUI 构造，以及选定的
Aura base action、PC 角色识别和任务复用契约。它不执行依赖真实游戏画面的完整
自动化流程；这部分仍需发布自检和实机验证。需要隔离运行时，可以显式使用
`.pytest_tmp/<scope>`，但不得把 `--basetemp` 指向仓库外：

```powershell
New-Item -ItemType Directory -Force -Path .pytest_tmp | Out-Null
python -m pytest tests\smoke -q --basetemp .pytest_tmp\smoke
```

非 pytest 工具如果会调用操作系统临时目录，运行前必须把临时环境变量指向仓库内的
专用目录，例如：

```powershell
$testTemp = Join-Path $PWD ".pytest_tmp\manual"
New-Item -ItemType Directory -Force -Path $testTemp | Out-Null
$env:TEMP = $testTemp
$env:TMP = $testTemp
$env:TMPDIR = $testTemp
```

日常验证应使用 `.\.venv\Scripts\python.exe`，不要从历史 `.venv-*` 或
`.runtime-*` 目录中挑选解释器。

## CLI Smoke

```powershell
.\scripts\run_cli.ps1 games --all
.\scripts\run_cli.ps1 tasks resonance
.\scripts\run_cli.ps1 tasks resonance_pc
```

## 发布版本号

- 源码中的应用版本使用 `vX.X.X`，例如 `v1.12.1`。同一基础版本的测试包沿用该源码版本。
- 正式包、Git 标签和 GitHub Release 标题使用 `vX.X.X`。测试包使用 `vX.X.X-E.N`，其中 `N` 是不带前导零的正整数。
- 测试版以最新正式版为基础，编号从 1 开始递增。例如 `v1.12.1-E.2` 之后发布 `v1.12.1-E.3`，切换到新的正式版本后重新从 `E.1` 开始。
- GitHub Release 标题与标签完全一致。功能说明写在发布说明里，平台和构建类型按打包约定写在附件名里。例如 `AuraResonance-v1.12.1-E.3-win-x64-cpu.zip`。
- 编号测试版以 GitHub 预发布形式进入测试版更新通道，正式版通道继续指向正式版本。

## Release Smoke

PR 和普通 `main` 推送运行源码 CI。深潜、货运和贸易及其下游功能按
`.github/ci-test-groups.json` 选择测试，并运行基础冒烟。白名单内的纯文档改动
跳过测试。共享组件、未映射路径或缺少可靠基准的变更执行全量。新增映射时应同时
考虑源码、任务、模板、fixtures 以及使用共享功能的测试。路径使用正斜杠，
通配符 `*` 可以跨目录匹配。

编号实验版优先复用同一提交已通过的 `main` 源码 CI。同提交的 CI 正在运行时最多
等 3 分钟，随后从最近通过 CI 的 `main` 祖先补跑增量，缺少基准时执行全量。
正式版本标签和带正式版本号的手动构建执行全量源码 CI。

版本标签和手动 `scope=full` 的 CPU 打包与源码 CI 并行。发布要求源码 CI、
CPU 构建和产物校验成功。每份 CPU 包都执行冻结运行时、GUI、更新器、ZIP 和校验和
检查。只跑全量源码 CI 时手动选择 `scope=ci` 和 `test_scope=full`。预演打包
时选择 `scope=full` 和 `publish_release=false`，并通过 `test_scope` 指定
源码测试范围。每次作业的摘要列出比较提交、选组原因和实际测试耗时。

启用 VPS 下载源时，将 HTTPS 站点地址保存在仓库的 Actions secret
`AURA_UPDATE_VPS_URL` 中。允许发布的构建会将地址编码到 `更新.exe` 的资源里。
使用专用 CA 时，把 CA 的公开 PEM 证书保存为 Actions secret `AURA_UPDATE_VPS_CA_PEM`。
更新器直连 VPS，并仅在这条连接使用专用 CA。GitHub 回退沿用系统默认信任和代理。
本地构建或试跑可通过同名环境变量指定地址。没有配置地址的构建直接使用 GitHub。
地址编码用于避免源码扫描，发行包中仍可还原该地址。

VPS 上用 `python -m scripts.release.serve_update_mirror` 启动下载服务。
服务读取镜像目录中的 `channels/stable.json` 和 `channels/experimental.json`，
索引包含 `tag`、`size`（字节）和 `sha256`。先下载并核对 GitHub 的校验清单，
再发布 `releases/download/<tag>/AuraResonance-<tag>-win-x64-cpu.zip` 和对应索引。
两个通道使用同一个服务进程，安装包通过下载队列交付。
用 `python -m scripts.release.sync_update_mirror --mirror <镜像目录>` 同步两个通道。
各通道校验新包成功后才切换索引，并清理该通道的旧 ZIP。

行为单元套件输出最慢的 15 项耗时，后续优化以实际慢项和重复断言为依据。
已合并跨文件的同一几何搜索、原生眼睛素材直角遍历，以及已有精确场景断言覆盖的
商店负例；保留非直角样本、真实截图、数值边界及故障路径。

本地打包统一从高层入口开始，它会按 profile 创建并复用隔离的发布环境：

```powershell
.\scripts\package_release.ps1 -Profile cpu -ReleaseLabel local
.\scripts\package_release.ps1 -Profile gpu -ReleaseLabel local
.\scripts\package_release.ps1 -Profile all -ReleaseLabel local
```

`build_release.ps1` 是内部目录构建器，不应作为本地或 CI 的发行入口。

```powershell
pwsh -NoProfile -File <release>\run.ps1 games --all
pwsh -NoProfile -File <release>\run.ps1 tasks resonance
pwsh -NoProfile -File <release>\run.ps1 tasks resonance_pc
pwsh -NoProfile -File <release>\run.ps1 doctor --ocr --ocr-provider cpu
& <release>\runtime\AuraResonanceRuntime.exe --self-check
& <release>\更新.exe --self-check
```

When GUI packaging is enabled, the release root should contain `AuraResonanceGui.exe` and `runtime\AuraResonanceRuntime.exe`.
