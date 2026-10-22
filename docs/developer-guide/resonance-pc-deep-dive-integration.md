# 深潜规划与四视图集成

深潜包含单局、完整循环、布局扫描、数据采集、灵敏度探测、离线规划和关卡内自动运行。使用方式见[完整循环](resonance-pc-deep-dive-loop.md)、[规划器](resonance-pc-deep-dive-planner.md)及[关卡内自动运行](resonance-pc-deep-dive-planned-run.md)。

## 运行边界

- 规划任务读取保存的布局，不进行游戏输入；关卡内执行任务需要用户主动启动。
- 扫描支持显式 `scan_route=four_views`，默认路线及各安全门保持源码定义，不因本次合并放宽识别或执行条件。
- 实体模型使用现有 CPU/auto 后端。`dml_worker` 是可选隔离进程，需要显式配置外部 Python、运行库和设备，CPU 完整包不内置该运行库。
- 模型、模型 metadata 和四视图几何均为运行资产，发布计划组装器会精确收集；训练输出、用户缓存和研究原图仍不进入包。
- `research/` 仅用于离线算法验证和显式研究工具，生产扫描不依赖研究模块。参考标注和图片须由使用者提供，缺失时在输入操作前失败。

## 四视角诊断日志

`scan_route=four_views` 每次扫描在原输出目录内新增以下文件：

- `control_trace.jsonl`：按事件追加记录 `run_id`、`scan_epoch`、`cid`、捕获来源及时间，包含 `arrival_decision`、`feedback`、`drag_start`／`drag_release`／`drag_observed`、`reference_transition`、`view_witness` 和 `scan_stop`，用于关联取景判断、拖动、参考图切换与实际观测。记录步长与局部响应斜率、图像误差及格框位移、按下／松手时间、帧间隔和处理耗时；`same_error_reference` 为假时不计算跨参考的误差变化率。
- `diagnostics.json`：记录版本、源码与几何资产哈希、配置、结果摘要及日志写入故障状态。
- `failure_frame.png`：失败或取消时尽力保存最后已有图像，不额外截图。

原有 `frames/`、`feedback.json`、`geometry_view*.json` 等证据继续保留。独立扫描目录为 `logs/deep_dive_scan/<run_id>/`；关卡内自动运行的扫描目录为 `logs/deep_dive_planned_run/<session_id>/scans/<序号>/`。

日志只观测现有流程，不新增截图、推理、等待或游戏输入，不改变阈值及控制策略。新增事件采用追加写入，鼠标按下期间不落盘诊断日志；日志写入仍有开销，摘要会记录累计写入耗时。诊断写入失败时记录故障并尽力继续原任务。日志中的图像误差角是图像上的方向误差，不代表魔方物理旋转角；三帧记录只描述现有观测，不声明镜头已经停稳。新增日志用于后续实机排查，不能单独作为识别准确或取景稳定的验收结论。

## 验收边界

离线样本与模拟采集不代表真实游戏流程验收；四视图识别和完整玩法仍需在同一冻结版本独立实机验收。

当前 DML 部署方式见[隔离运行时说明](resonance-pc-deep-dive-dml-runtime.md)，研究工具输入要求见 [research README](../../research/README.md)。
