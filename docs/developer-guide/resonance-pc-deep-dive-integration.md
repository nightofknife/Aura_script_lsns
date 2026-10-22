# 深潜规划与四视图集成

深潜包含单局、完整循环、布局扫描、数据采集、灵敏度探测、离线规划和关卡内自动运行。使用方式见[完整循环](resonance-pc-deep-dive-loop.md)、[规划器](resonance-pc-deep-dive-planner.md)及[关卡内自动运行](resonance-pc-deep-dive-planned-run.md)。

## 运行边界

- 规划任务读取保存的布局，不进行游戏输入；关卡内执行任务需要用户主动启动。
- 扫描支持显式 `scan_route=four_views`，默认路线及各安全门保持源码定义，不因本次合并放宽识别或执行条件。
- 实体模型使用现有 CPU/auto 后端。`dml_worker` 是可选隔离进程，需要显式配置外部 Python、运行库和设备，CPU 完整包不内置该运行库。
- 模型、模型 metadata 和四视图几何均为运行资产，发布计划组装器会精确收集；训练输出、用户缓存和研究原图仍不进入包。
- `research/` 仅用于离线算法验证和显式研究工具，生产扫描不依赖研究模块。参考标注和图片须由使用者提供，缺失时在输入操作前失败。

## 验收边界

离线样本与模拟采集不代表真实游戏流程验收；四视图识别和完整玩法仍需在同一冻结版本独立实机验收。

当前 DML 部署方式见[隔离运行时说明](resonance-pc-deep-dive-dml-runtime.md)，研究工具输入要求见 [research README](../../research/README.md)。
