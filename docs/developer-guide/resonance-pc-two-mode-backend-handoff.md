# 两模式货运后端交接

现行规范：[两模式契约](resonance-pc-two-mode-freight-design.md)。GUI由“gui调整”线程接手；本交接描述后端、任务、共享参数投影及离线验证。

## 已完成

- solver/service/action 默认 `trade_mode=free`，仅接受 free/fixed，旧 profit/quick/target 运行模式拒绝。
- free 无目标为最大收益，有正 `target_profit` 为最低疲劳达标；fixed继续预算内最大疲劳、允许最后部分圈、定位独立且计入总预算。
- fill为独立用书策略，不再强制协商；有限/无限书及单本严格正且>=阈值规则保持。
- 不用书时无效 book_policy/book_profit_threshold 不影响计算或缓存；固定模式忽略目标收益、自由城市和终点。
- auto_cycle_trade_pc、preview_trade_plan_pc、auto_combined_commerce_pc 的模式枚举/default以及nullable目标均已同步，manifest已通过源码生成器同步。
- 共享模块 `services/resonance_pc_freight_parameters.py` 提供 `FREIGHT_MODES / FREIGHT_PLANNING_KEYS / effective_freight_inputs / freight_control_state`。GUI可以直接依赖纯模块，不需要导入actions。
- `target_profit_enabled`是GUI保存字段，不是后端任务参数。GUI关掉目标后提交null/省略；正目标按原精确算法处理。
- 返回字段、定位、访问索引、状态与停止原因保持现行执行契约。目标不可达状态仍叫target_unreachable，不表示第三种模式。

## 验证

347 passed，8.08秒。命令：

```powershell
.venv/Scripts/python.exe -m pytest tests/unit/test_resonance_pc_freight_mode_solver.py tests/unit/test_resonance_pc_freight_planner_service.py tests/unit/test_freight_parameter_dependencies.py tests/unit/test_freight_modes_runtime.py tests/unit/test_resonance_pc_book_budget_flows.py tests/contracts/test_resonance_pc_book_budget_contract.py tests/unit/test_freight_progress_contract.py tests/unit/test_freight_recovery_policy.py tests/unit/test_freight_recovery_integration.py tests/unit/test_combined_commerce_end_city.py -q --basetemp .pytest_tmp/two_freight_modes/backend_pytest
```

系统TEMP/TMP/TMPDIR使用另一个仓库内目录 `.pytest_tmp/two_freight_modes/os_temp`。测试不操作游戏。250组随机小图分别检查收益、目标、fill+协商和固定疲劳目标（1000组组合），包括完整平局与适用dense/sparse对照。

package check/validate通过；`git diff --check`通过（共享工作树含其他任务改动）。静态Doctor结果在 `.pytest_tmp/two_freight_modes/plan-doctor.json`，0 errors；179项警告均为现有pyc/pycache产物，未清理共享缓存。

## GUI 联合验收（2026-10-14）

“gui调整”线程已完成 GUI，详见 [GUI交接](resonance-pc-two-mode-gui-handoff.md)。主线程复核新配置边界、参数投影、总运行按钮校验，并查看1180×720的不定线路收益目标页和固定线路截图。

最终使用三个独立pytest进程验证，共 **657 passed**，三组文件不重叠：

| 分组 | 结果 | 原始报告 |
| --- | --- | --- |
| 后端及GUI核心联动 | 529 passed，101.91秒 | `.pytest_tmp/two_freight_modes/core-results.xml` |
| 整窗及紧凑布局 | 52 passed，41.94秒 | `.pytest_tmp/two_freight_modes/layout-results.xml` |
| 投资GUI及拣货间隔 | 76 passed，5.86秒 | `.pytest_tmp/two_freight_modes/extra-results.xml` |

核心组为上方10个后端文件，加 `test_freight_modes_gui.py`、`test_two_mode_freight_gui_dependencies.py`、`test_trade_page_end_city.py`、`test_auto_book_gui_restore.py`、`test_commerce_parameter_sync.py`、`test_workflow_recovery_refresh.py`、`test_workflow_progress_contract_gui.py`。布局组为 `test_workflow_workspace_gui.py`、`test_compact_parameter_grid_gui.py`；附加组为 `test_trade_goods_investment_gui.py`、`test_pickup_interval.py`（均位于 `tests/unit`）。所有组从仓库根运行，使用 `QT_QPA_PLATFORM=offscreen`、`PYTHONDONTWRITEBYTECODE=1`、`-o faulthandler_timeout=60`，各组basetemp独立且在 `.pytest_tmp/two_freight_modes/` 内。

最初将全部GUI文件放同一进程时，在后置布局夹具切换全局Qt样式处长时间停顿，已停止那两次未完成运行，不计入通过数。第二次的faulthandler栈明确位于 `test_trade_goods_investment_gui.py:30` 的 `app.setStyle("Fusion")`；存在前序窗口未彻底销毁导致样式重置成本累积的风险，未宣称完整根因已查明。将全局样式初始化组置于独立进程后全部通过，没有为此修改产品逻辑。657项是分组完整运行结果，不是把中断运行或GUI线程早期定向重跑相加。

最终重新执行package check/validate均通过。所有验证为离线算法、契约、记录型派发与Qt控件/截图验证，不代表实机跑商或不同系统DPI的验收。

本次未提交、推送、打包、发版或实机跑商。
