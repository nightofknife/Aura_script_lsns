# 独立客运与客货联合计算退役

本次仅保留货运中的附加客运：在不定线路货运参数中开启，沿既有货运路线揽客、派发和结算，不改变货运求解、疲劳预算、恢复、拖车或终点清仓。

## 删除范围

- 工作流程中的独立「客运」任务、A/B 端点、单程数、独立客运参数页及运行入口。
- 独立客运进度事件、独立行程状态和 GUI 路线疲劳目录。
- 相邻货运与客运任务的联合编排、先客后货/先货后客顺序、联合预算预留与重新计算。
- `auto_passenger_trips_pc.yaml`、`auto_combined_commerce_pc.yaml` 两个任务及其专用运行编排和清单导出。
- 使用教程、用户指南和现有用例中针对上述入口的现役说明与断言。

## 保留边界

- 货运 `auto_passenger` / `passenger_options` 输入、UI、试算预计收入及执行状态不变。
- `_freight_passenger_planner.py`、`_freight_passenger_session.py` 以及共享的揽客、余额、目的地识别和结算动作保留。
- `templates/passenger/`、目的地目录、客运收入模型、原生目的地与数值模型构建工具均由附加客运继续使用。
- 城市行车疲劳、到站确认、恢复、拖车、货物投资等共享货运功能不随独立入口删除。
- 附加客运在取消或提交结果未知时继续保留未结清目的地，禁止盲目重复揽客。

## 已保存设置与历史

旧工作流中的独立 `passenger` 节点和退役任务引用不得在新版本重新进入待执行队列；其余有效任务顺序和参数应保留。旧独立客运设置不再参与读取、预算或派发。货运内部的 `passenger_options` 不属于旧独立客运设置，不得按名称一并清理。

已有历史记录只读保留，允许查看原独立/联合任务的结果；历史解析不恢复执行入口，不提供退役任务重新运行。既有 INI/日志/运行历史不是发行资产，不因功能清理删除用户文件。

## 验证记录

2026-11-09 从仓库根目录完成自动回归：

- 附加客运规划、批次状态、货运集成、目的地识别、传单余额与到站/结算边界：240 项通过。
- 四任务工作流、参数冻结、恢复前置流程、布局和旧设置兼容：123 项通过。
- 更新退役入口、界面挂载接口和现役任务清单的旧断言后，相关文件复跑：80 项通过。
- 最终完整 `tests/` 回归：**8001 项全部通过，0 失败、0 错误、0 跳过，耗时 361.69 秒**。包括任务发现、GUI 启动与布局、普通货运、附加客运、恢复、战斗、生产和其他独立功能。

最终命令（环境变量 `QT_QPA_PLATFORM=offscreen`，`TEMP`、`TMP`、`TMPDIR` 均指向仓库内 `.pytest_tmp/passenger-retirement/tmp`）：

```powershell
.venv/Scripts/python.exe -u -m pytest -v --tb=short --durations=12 --basetemp=.pytest_tmp/passenger-retirement/final-temp --junitxml=.pytest_tmp/passenger-retirement/final.xml
```

最终完整日志保存在 `.pytest_tmp/passenger-retirement/final.log`，JUnit 报告保存在 `.pytest_tmp/passenger-retirement/final.xml`。GUI 检查生成的截图保存在仓库内 `.pytest_tmp/gui-layout-restore/`；已查看最小窗口下的货运页面，四个工作流节点及参数/运行区布局正常。

以上为自动化离线回归，未进行真实游戏实机操作。
