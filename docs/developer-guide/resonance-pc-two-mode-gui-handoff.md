# 两模式货运 GUI 交接

日期：2026-10-14。状态：GUI 实现和针对性离线验证完成，可进行 GUI＋后端联合回归。

工作区：`D:\python_project\Aura_script_lsns`，分支 `codex/deep-dive-planned-run-20260930`，接手及交付 HEAD 为 `f286710`。本轮原地保留共享工作树中的其他修改，没有提交、推送、打包、发版或操作游戏。不要将共享工作树全部改动当成本轮交付范围。

后端规范见 [两模式契约](resonance-pc-two-mode-freight-design.md) 和 [后端交接](resonance-pc-two-mode-backend-handoff.md)。本轮没有编辑 services、solver、actions、tasks 或 manifest。

## 已实现

- 入口只显示不定线路 `free` 与固定线路 `fixed`，默认 `free`。不定线路的收益目标为独立开关，开启后必须正金额；关闭保留填写金额但不提交目标。固定线路隐藏城市范围、终点和收益目标，保留草稿。
- 用书三状态：不用书时本数可见但禁用，阈值/策略禁用；有限时全部启用；无限时数量位置显示“无限”，保留原有限书数。`fill` 文案为“尽量满仓”，不强制协商或无限用书。
- 不协商时尝试次数禁用；便当关闭时类别和排序禁用；汽水和便当都关闭时恢复下限禁用；用药关闭时药品和次数禁用。用药控件统一放入一个紧凑网格，修复次数输入框被两个布局重复引用而脱离禁用区的问题。
- 运行快照使用共享 `effective_freight_inputs`，保存使用未投影的界面草稿；运行只传当前适用字段。联运入口也保存原始草稿，不再用运行投影覆盖隐藏值。快照与规划指纹对嵌套数据做深拷贝。
- 允许保存未完成的固定线路、未填完的目标、空城市集合、空手动终点以及暂不完整的便当选择。运行前另行校验。隐藏/置灰以及切页不清空设置。
- 被移出范围的已选终点仍保留，可直接取消；不自动取消终点或切到自动终点。冲突时参数区有提示，试算、货运启动和选中货运时的流程总运行按钮均禁用。取消勾选货运后不会阻止其他任务；运行期间的停止按钮不会因参数错误被禁用。
- 固定线路显示闭环/开放提示；相邻重复城市同时在按钮状态与实际提交边界拒绝。试算起点被移出城市范围时保留显示并提示，正式运行仍不提交试算起点。
- 有效规划参数、城市声望、商品解锁变化会将已有预览标为“需要重新试算”。参数页和结果页均显示提示；再次计算会清除旧提示。账号数据刷新成功也会使已有试算失效。投资、倒垃圾等执行开关不触发重算。
- `_planning_key` 直接读取共享纯模块的 `FREIGHT_PLANNING_KEYS`，不再导入 action helper。恢复表单时阻断主窗口的自动保存信号，防止部分恢复的便当、汽水或拣货设置被提前写回。

## 新配置边界

按最终要求，不兼容旧货运配置，不作旧模式映射、迁移或专用确认提示，不保留 Auto Book 保存格式转换。

货运和试算采用两个新保存键，仍位于程序现有 QSettings 容器中，不改变其他功能的配置文件/键：

| 用途 | 新键 |
| --- | --- |
| 货运草稿 | `trade/two_mode_inputs_json` |
| 独立试算草稿 | `trade_preview/two_mode_inputs_json` |

旧 `trade/inputs_json`、`trade_preview/inputs_json` 不作为新货运配置读取，也不删除或改写。新试算草稿首次从新货运配置初始化，之后独立保存。旧模式 `profit/quick/target` 不能写入新配置或传给新表单；旧 `auto_book` 字段不影响是否用书。新格式的 `book_budget=null` 与界面 `books_enabled/books_unlimited/finite_book_budget` 继续支持无限用书和有限数值恢复。

`target_profit_enabled` 仅保存 UI 状态，不进入任务输入。`book_policy` 中的 `profit` 是合法用书策略，与被取消的货运模式名无关。

## GUI 文件范围

实现（包括接手初稿）：

1. `packages/resonance_gui/widgets/trade_page.py`
2. `packages/resonance_gui/widgets/compact_parameter_grid.py`
3. `packages/resonance_gui/widgets/workflow_page.py`
4. `packages/resonance_gui/config_repository.py`
5. `packages/resonance_gui/logic.py`
6. `packages/resonance_gui/main_window.py`

新增/调整的 GUI 测试：

1. `tests/unit/test_two_mode_freight_gui_dependencies.py`（新增）
2. `tests/unit/test_freight_modes_gui.py`（共享树中原为未跟踪的初稿）
3. `tests/unit/test_trade_page_end_city.py`（接手的终点边界用例）
4. `tests/unit/test_auto_book_gui_restore.py`
5. `tests/unit/test_commerce_parameter_sync.py`
6. `tests/unit/test_trade_goods_investment_gui.py`
7. `tests/unit/test_workflow_workspace_gui.py`
8. `tests/unit/test_workflow_recovery_refresh.py`

`test_compact_parameter_grid_gui.py` 与 `test_pickup_interval.py` 一并验证，但本轮未编辑；后者包含既有拣货后端假时钟测试。其他未列出的共享树变化不属于本轮 GUI 修改。

## 验证结果与复现

所有命令从仓库根运行。系统临时目录、Python 缓存与 pytest basetemp 分离，均在 `.pytest_tmp/two_freight_modes_gui/` 内。

```powershell
$taskTemp = Join-Path (Get-Location) '.pytest_tmp/two_freight_modes_gui/temp-main'
New-Item -ItemType Directory -Force -Path $taskTemp | Out-Null
$env:TEMP = $taskTemp
$env:TMP = $taskTemp
$env:TMPDIR = $taskTemp
$env:PYTHONPYCACHEPREFIX = Join-Path (Get-Location) '.pytest_tmp/two_freight_modes_gui/pycache'
$env:QT_QPA_PLATFORM = 'offscreen'

.venv/Scripts/python.exe -m pytest `
  tests/unit/test_two_mode_freight_gui_dependencies.py `
  tests/unit/test_freight_modes_gui.py `
  tests/unit/test_trade_page_end_city.py `
  tests/unit/test_auto_book_gui_restore.py `
  tests/unit/test_commerce_parameter_sync.py `
  tests/unit/test_compact_parameter_grid_gui.py `
  tests/unit/test_trade_goods_investment_gui.py `
  tests/unit/test_workflow_workspace_gui.py `
  tests/unit/test_workflow_recovery_refresh.py `
  tests/unit/test_pickup_interval.py `
  --basetemp=.pytest_tmp/two_freight_modes_gui/final -q --tb=short `
  --junitxml=.pytest_tmp/two_freight_modes_gui/final-results.xml
```

最后整组运行：305 项，302 通过，3 项失败。失败均为布局测试残留的旧断言 `mode != "target"`，错误地要求 free 的目标金额字段隐藏；实际新界面应当显示该字段，并由开关控制是否可编辑。将断言改为 `mode == "fixed"` 后，未再修改实现代码，仅重跑相关 6 个布局用例，全部通过：

```powershell
.venv/Scripts/python.exe -m pytest tests/unit/test_workflow_workspace_gui.py `
  -k freight_parameters_reflow `
  --basetemp=.pytest_tmp/two_freight_modes_gui/layout-final -q --tb=short `
  --junitxml=.pytest_tmp/two_freight_modes_gui/layout-final-results.xml
```

因此当前没有遗留测试失败；305 个用例的通过证据来自整组运行与失败修复后的定向重跑，不声称最后重新跑过完整 305 项。两份 XML 保留原始结果。更早的新配置/控件/投影组合验证为 123 passed，不能与上述数量相加。

验证覆盖实际 GUI→记录型 Bridge 的派发参数、运行快照、新键读写/重启、禁用值保留、旧格式隔离、预览失效/重算、账号变化、无效终点、便当恢复、拣货间隔，以及真实 Qt 控件布局。记录型 runner/假服务不点击游戏。

## 截图与布局

截图目录：`.pytest_tmp/two_freight_modes_gui/visual-qa/`、`visual/`、`compact-fields/`。

- `visual-qa/trade-free-goal-1180x720.png`：不定线路、目标开启、无书状态。
- `visual-qa/trade-fixed-1180x720.png`：固定线路与线路编辑区域。
- `compact-fields/free-1180x720.png`、`free-1440x860.png`、`free-1920x1080.png`：参数列数与隐藏/禁用联动。
- `compact-fields/fixed-1180x720.png` 等：固定模式缩放布局。

已人工检查 1180×720 的不定线路目标页，配合自动化检查输入宽度、兄弟控件重叠及滚动可达性。整窗还覆盖 1440×860；参数区域覆盖 1920×1080。截图显示的是共享源树原有版本号，未修改版本号，不代表已发布版本。

## 交接结论

当前无需要后端新增的接口或阻塞项；共享纯模块已满足 GUI 使用。后续由主线程执行 GUI＋后端联合回归，再单独决定交付操作。这里没有冻结包、不同系统 DPI 或真实游戏验收结果。


## v1.11.9 主线集成说明（2026-10-14）

以上为源工作区历史验证。发布集成从 v1.11.8 主线提取两模式增量，保留三栏布局、`gui-settings-layout-v2.ini`、黑月采购表、投资可用状态和细粒度运行进度；不沿用源工作区的旧布局。两模式表单使用独立的 `trade/two_mode_inputs_json` 与 `trade_preview/two_mode_inputs_json`，不把旧四模式参数解释成新模式。冻结 GUI 自检已改为两模式及目标开关，同时保留三栏、返回货运和停止确认检查。

交付工作区单进程混合回归 232 项通过（143.88 秒），覆盖 solver/service/runtime、自动用书、终点、黑月、layout-v2 与完整工作区布局；新增依赖/草稿/过期提示 40 项此前通过。报告在 `.pytest_tmp/release-v1119/freight/`。这些是离线验证，不代表实机跑商通过；正式全集成验证以发布记录为准。
