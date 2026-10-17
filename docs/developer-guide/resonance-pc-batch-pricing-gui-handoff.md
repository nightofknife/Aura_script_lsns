# PC 货运整单结算：GUI 交接

日期：2026-10-18。工作区：`D:\python_project\Aura_script_lsns`。

## 范围与状态

GUI 部分已实现，离线验证完成。本轮在共享脏工作区中做局部修改，保留其他线程已有内容；没有推送、提交、打包、发布或操作游戏。

本轮实现文件仅为：

- `packages/resonance_gui/config_repository.py`
- `packages/resonance_gui/logic.py`
- `packages/resonance_gui/widgets/trade_page.py`
- `packages/resonance_gui/widgets/workflow_page.py`
- 新增 `tests/unit/test_batch_pricing_gui.py`
- 本交接报告

这些实现文件此前已有大量未提交改动，不能把整个相对 HEAD 的差异都算成本轮修改。未改后端 service/action/task/manifest。`FREIGHT_PLANNING_KEYS` 中的新字段由后端线程提供，GUI 继续引用该共享列表。

## 城市实际税率

沿用城市声望弹窗，入口改为“设置声望与税率”，弹窗包含“城市声望／实际税率”两个页签，无新主页面。

- `city_tax_rates_bps` 默认 `{}`，未覆盖城市按后端声望／规则计算。
- 税率按城市单独开启覆盖，输入范围 0.00%～100.00%，两位小数；通过十进制换算为整数 bps，7.50% → 750，0% 是有效覆盖。
- 契约严格验证字典、非空字符串键及 0～10000 的整数值，不把布尔、小数或字符串静默转成整数。
- UI 明确说明覆盖值就是最终实际税率，不额外叠加声望减税。
- 声望页说明父城／势力继承关系；子城名称的提示说明归属，12／18 说明其声望为父势力代理。未改后端继承算法。
- 关闭某城覆盖后，在当前弹窗内保留数值但禁用输入；保存时从有效覆盖字典移除该城，回到规则税率。取消弹窗不修改参数。
- 切模式、切页面或取消某城参与规划，不删除已保存的税率覆盖。运行和预览均使用复制后的字典，运行快照不受后续编辑影响。
- 新字段进入货运、联跑货运子输入、独立试算草稿和试算提交，不进入客运。
- 独立试算首次从货运配置初始化，之后与货运草稿独立；本轮未增加旧配置迁移。
- 税率改变通过共享规划字段进入指纹并使已有试算失效；未改 `fill`、浪费上限、收益优先阈值的现行逻辑。

保存失败不宣称成功：货运与试算配置保存复用 `sync_checked()`，失败时恢复此前 JSON 并抛出错误；弹窗显示“保存失败”，保持原有内存税率。读取已保存的非法税率会报告错误，不静默覆盖整个草稿为默认值。

## 预计净收益与明细

路线表及正式运行路线卡片使用“预计净收益”。可见说明指出已计入买入税和卖出利润税，游戏显示的税前利润不等于净收益，预计值不是实际已确认收益。

`freight_settlement_lines()` 只展示后端每条路线的 `settlement`，不在 GUI 计算或推导税额。已按当前后端 `Settlement.to_dict()` 接口接入：

| 字段 | 展示 |
| --- | --- |
| `buy_total` | 买入支出 |
| `buy_tax` | 买入税，包含在买入支出中 |
| `sell_total` | 卖出到账 |
| `sell_tax` | 卖出利润税，已从到账金额中扣除 |
| `taxable_profit` | 计税利润，注明未扣买入税、不等于净收益 |
| `net_profit` | 预计净收益 |

有 settlement 时，路线表提供可展开子行和收益列悬停明细；运行路线卡片直接展示同一组明细。无 settlement 不造数据；部分字段缺失显示“未提供”，不会靠买卖金额倒算净收益。所有此类明细标注“预计结算（未实测）”。

用户样本展示：买入支出 1,472，其中买入税 96；卖出到账 1,568，卖出利润税 16；计税利润 208，预计净收益 96。该样本用于展示测试，不构成新增实机验证。

## 异币城市

沿用后端确认的错误消息：

> 武林源等异币城市尚不支持统一货运收益计算，请移出线路或参与城市。

GUI 现有默认参与城市本来就不含 21，增加断言保护。旧草稿中的 21 不删除、不迁移：

- free 的适用参与城市／终点包含 21 时阻止启动。
- fixed 的实际线路包含 21 时阻止启动；不因未生效的 free 城市草稿包含 21 而拦截有效 fixed 线路。
- 试算起点为 21 时阻止试算；实际起点由执行端读取的场景仍由后端校验。
- 参数区显示冲突，现有 validationChanged 链同步控制运行按钮；后端仍负责最终校验，不扩展异币功能。

## 离线验证

所有命令从仓库根执行，使用 `.venv/Scripts/python.exe`，`QT_QPA_PLATFORM=offscreen`。TEMP/TMP/TMPDIR 分别设到本仓库 `.pytest_tmp/batch-pricing-gui/temp` 或 `temp-layout`，PYTHONPYCACHEPREFIX 设到 `.pytest_tmp/batch-pricing-gui/pycache`。

第一组，**163 passed**（38.16 秒），包含本轮新文件 **42 项**：

```powershell
.venv/Scripts/python.exe -m pytest tests/unit/test_batch_pricing_gui.py tests/unit/test_freight_modes_gui.py tests/unit/test_two_mode_freight_gui_dependencies.py tests/unit/test_auto_book_gui_restore.py tests/unit/test_trailer_gui.py -q --tb=short --basetemp=.pytest_tmp/batch-pricing-gui/core --junitxml=.pytest_tmp/batch-pricing-gui/core.xml
```

第二组为独立 Python 进程，避免已知 Qt 全局样式切换的累计窗口风险，**56 passed**（41.93 秒）：

```powershell
.venv/Scripts/python.exe -m pytest tests/unit/test_workflow_workspace_gui.py tests/unit/test_compact_parameter_grid_gui.py tests/unit/test_trade_page_end_city.py -q --tb=short --basetemp=.pytest_tmp/batch-pricing-gui/layout --junitxml=.pytest_tmp/batch-pricing-gui/layout.xml
```

两组共 **219 项通过**，无重叠文件。早期首轮 22 项已被第一组覆盖，不重复计数。限定文件 `git diff --check` 通过。

新用例覆盖严格校验、百分比与 bps、0% 覆盖、保存与重新读取、运行快照、free/fixed、预览隔离与失效、取消／保存失败回滚、武林源草稿保留与适用字段拦截、样例 settlement、缺失字段不推算，以及税率弹窗滚动可达性。

已检查离屏截图 `.pytest_tmp/batch-pricing-gui/core/test_city_dialog_tax_tab_is_re0/city-tax-overrides.png`：700×600、中文字体、税率页无横向滚动、所有城市可纵向滚动到达。此图用于内容和布局检查，不是冻结包、系统 DPI 或实际主题验收。

## 后续边界

后端线程继续负责整单计税、取整、缓存和求解正确性、任务接口及生成 manifest。GUI 不读取游戏税率，不执行交易，不把预计结算当已确认账目。正式集成、冻结包和真实游戏验收尚未在本轮执行。
