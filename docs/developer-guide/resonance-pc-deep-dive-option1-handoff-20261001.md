# 识海深潜方案 1：暂停交接（2026-10-01）

用户已选择方案 1，并要求暂停研究和实机测试，将现有进度上传分支，后续继续开发。

分支：`codex/deep-dive-planned-run-20260930`。本次是 **开发进度快照，尚未达到完整验收**，不合并。恢复工作应从本文件开始；先解决下列配准卡点，再开展实机测试。

## 目标与当前结论

目标仍为：单次识别 ≤60 秒、平均 ≤50 秒、正常约40秒；玩家、奇点、灵感的最终所属格准确率稳定 ≥99%。不得通过延长预算、放松配准门槛、只统计成功样本来宣布达标。

方案 1 已接入生产任务：实体检测模型定位目标 → 当前帧图案校正几何 → 六面54格竞争关联与多视角投票 → 目标清单闭合后停止 → 普通移动目的格按需补读。普通图案未知仍保留 unknown，不再等54个图案全部读齐。

**目标扫描已有成功实测；完整“扫描→规划→补读→定位动作”尚未跑通。99%尚无独立验收。** 最新扫描43.062秒、六面已观察、38/54普通图案已知，四个目标正确；后续普通宽视角配准未通过，流程在累计识别57.468秒主动阻塞。任务框架的 `success` 只表示节点执行正常，不能当作业务成功。

## 已落地代码

- `src/services/deep_dive_entity_detector_service.py`：注册单例模型服务，懒加载、串行推理、精确一帧缓存、明确 provider/覆盖有效性；失败不回退成“空目标”。模型框负责实体完整包络，粉色头部规则仅作局部支持；棋子身体假圆只作遮挡。
- `_deep_dive_layout_vision.py`：只有同帧实际图案校正成功才允许目标正票；正面归属必须胜过全部54格的有限法向走廊。背面仅作竞争否决，不吸附成正面格子。已修复一次真实 Boss 框中心端点误归属，回放45条Boss证据保留44条正确D11，拒绝唯一错误D01；这不是泛化准确率证明。
- `_deep_dive_target_readiness.py`：明确 `targets_ready` 与 `layout_complete`。六面必须有真实成票；玩家/奇点唯一、至少两个真实视角；灵感用HUD“总量−已收集”作上界，Boss吃掉灵感时不能强求等于HUD上界。低于上界时需要54格占用全部确认。
- `_deep_dive_scan_stream.py`：接受结果时冻结同源语义帧、姿态和地图版本；停止排空不能用新姿态重新解释旧目标框。几何与语义地图版本分开，避免旧锚点误成为恢复目的地。
- `_deep_dive_scan_policy.py`：目标模式忽略无关普通图案缺口，未见面优先；沿真实30°路径检查点比较较近的新面读取机会，收益饱和、按实测角速度估计路线成本。full/layout保留旧行为；原锚点和成票门槛不变。
- `_deep_dive_movement_planner.py`、`_deep_dive_directed_operation_flow.py`：接受已证明的目标清单，未知普通目的格必须补读后才进入输入绑定；预测地图不能操作。
- `_deep_dive_required_cell_reader.py`：当前真实配准、所有Q一致、无实体遮挡、三份新截图跨至少0.4秒、稳定同一图案后，仅更新需要的目的格。补读后允许把同一帧配准扩展到新的节点摘要，保留跨帧稳定性；**这项消除重复配准的最后修改仅通过回归，暂停前未完成新实机验证**。
- `consciousness_deep_dive_planned_run_pc_actions.py`：初始扫描、宽视角配准、补读和动作前核验注入同一模型服务；累计识别预算保留，不增加时长。
- `_deep_dive_operation_frame.py`：宽视角、移动/旋转视角、旋转预览均可注入当前RGB的目标检测器；异常/无效覆盖阻止操作，不能回退旧规则。

本次快照也包含前一轮已完成的页面精确ROI缓存、掩膜NCC、批量投影、暖色/紫环/眼图案修复、多面图案锚点、短路径与恢复保护及对应回归，详见先前[候选方案报告](resonance-pc-deep-dive-recognition-options-20261001.md)。本文件的接入状态优先于该报告的历史状态。

## 实机记录与计时边界

均为同一个第一位面棋盘的重复测试，不能当独立准确率样本。固定真值为玩家U11、奇点D11、灵感F22/B12，来自前次实际层旋转后独立47个普通图案一致性核验。

| 运行 | 扫描秒数 | 结果/后续 |
|---|---:|---|
| live_scan_01 | 启动失败 | WGC首次采集未启动导致等待新帧超时，已修复 |
| live_scan_02 | 43.578 | 排空后结果与旧目标框/新姿态混用，已修复冻结 |
| live_scan_03 | 45.078 | 棋子身体圆当成第二玩家，已修复同球头支持 |
| live_scan_04 | 55.922 | 历史一次Boss错格阻止闭合，已修复端点中心风险否决 |
| live_scan_05 | 29.046 | targets_ready；29/54图案；包含HUD与冷启动的调用32.671秒 |
| planned_binding_01 | 59.265 | D面没有真实成票，扫描失败；此后修改目标路线 |
| planned_binding_02 | 55.235 | targets_ready，复位后累计59.250秒预算阻塞 |
| planned_binding_03 | 52.094 | targets_ready，后续累计58.765秒预算阻塞 |
| planned_binding_04 | 43.062 | 新路径前缀策略；targets_ready；宽视角仍未配准，累计57.468秒阻塞 |
| planned_segment_02 | 不含扫描 | 重用最后实际未变棋盘地图的**分段测试**，目的格补读成功；后续移动视角配准失败 |

最后分段测试为检验后续模块而把扫描计时起点设为0，不能计入整段性能；补读及两轮宽视角配准累计28.249秒。测试原计划在目的格点击前截断，实际没有走到这个截断点，没有选择目的格或消耗移动额度。`planned_segment_01`只是测试helper取不存在字段导致的KeyError，也保留在记录中。

43.062秒仅为初始扫描阶段。planned_binding_04任务节点全调用66.179秒，**不能声称严格整段≤60秒已经实现**。最终版本只有一次新路径前缀实机记录，不得将多个不同版本的成功结果混合平均为最终版本成绩。CPU模型30帧推理平均75.68ms/P95 98.45ms；它也不代表整段耗时。

## 下一步按优先级处理

1. **修复操作模块与分类器的暖色中心掩码漂移。** 保存的真实移动视角失败图里，slot5红单眼已由当前分类器确认，置信度0.813/0.810；操作模块仍用旧红色掩码，中心像素为空，只剩4个顶部读数，达不到原5读数门槛。模型与旧规则回放均同样失败。研究内存覆盖对齐当前暖色支持后，同图原配准门槛得到6匹配、0冲突、唯一Q、consensus1.0，实际0.326秒。**生产修复未实施**。不要直接扩大颜色范围就接受墙面/接缝；只能在独立图案分类已确认后使用一致的像素支持，增加真实图及暖色墙面/接缝/Boss特效的拒绝回归。详情见[已冻结诊断](deep-dive-recognition-20261001/option1/operation_segment02_handoff.md)。
2. **定位最新目标扫描后普通宽视角配准失败。** planned_binding_04最后原因 `wide_three_face_geometry_or_content_unconfirmed`。目标模式允许38/54图案未知，不能假定重置后的三个操作可见面仍有足够已知图案。区分颜色中心漂移、实际图案缺失、遮挡与Q歧义。用其他面已知图案辅助，或只补充操作注册需要的图案；保留≥7锚点、三个面各≥2、consensus≥.88、RMSE≤6及稳定新截图。补读本身依赖有效配准，不能陷入“要补读才能配准、要配准才能补读”的循环。
3. **验证最后的补读配准扩展。** 重跑实际普通目的格未知的流程，检查三份不同WGC来源、同帧target masks、所有Q一致、引用摘要与map_revision同步；动作前仍用新截图重新绑定，不能复用旧点击坐标。此前确实成功补读了slot1的蓝鳞片，但最新省重复配准代码尚未实机。
4. **重新统计最终版本性能。** 顺序至少多次整段运行，不叠加旧测试helper/抓帧后台；至少一次使用生产默认OpenCV线程配置。此前helper设置 `cv2.setNumThreads(1)`，不可当所有机器默认性能。分别记录初始扫描、HUD/冷启动、复位/注册、补读、规划及调用总时长，包含失败。关闭测试runner后检查本测试Python是否真正退出，避免遗留WGC线程影响下一轮；不要终止游戏或其他用户进程。
5. **独立准确率验收。** 不同棋盘/帧率/视角、灵感被Boss吃掉或被拾取、Boss移动、目标遮挡均需人工独立真值。类别+所属格一起评分，漏检、错格、重复、未知/超时均记录。同棋盘连续帧、与旧规则一致都不能证明99%。

## 跨机器恢复

模型已随分支打包：`plans/resonance_pc/data/models/deep_dive_entities.onnx`，10,566,854字节，SHA256 `bff7444233525475ef25173845a6c197c1eec993f2544d204a44846260718123`。默认服务加载这个路径，不依赖`.pytest_tmp`或原研究环境。相对路径按仓库根解析。原研究权重不删除。

CPU依赖参考 `requirements/optional-vision-onnx-cpu.txt`；DirectML需要运行Aura的同一解释器安装`onnxruntime-directml`。不要在同一环境混装多个ORT发行包。配置、provider和模型契约见[服务文档](deep-dive-entity-detector-service.md)。这台机器实际生产测试使用CPU；CUDA缺DLL回退情况不代表另一台机器配置。

关键证据已经复制到版本化的 [option1证据目录](deep-dive-recognition-20261001/option1/progress_results.json)，包括失败移动画面、真实layout和registration_frame、模型/路径/错格诊断，不依赖本机pytest目录。全部原图、训练与逐帧日志仍留本机`.pytest_tmp/deep_dive_option1_20261001/`和`.pytest_tmp/deep_dive_perf_20261001/`，未全部上传。外部视频/下载素材也未搬入分支。

离线复现操作失败：读取`option1/operation_failure_snapshot.json`及同目录`operation_failure.png`，将PNG转RGB，调用`build_operation_frame(rgb, snapshot['layout'], 'move', scan_epoch=snapshot['scan_epoch'], map_revision=snapshot['map_revision'], view_epoch=snapshot['pose_epoch'], registration_frame=snapshot['registration_frame'], target_detector=service.detect_packet)`。原版预期等待/失败，修复后应在不降低门槛的情况下注册成功；这不应触发游戏输入。

生产入口仍为扫描任务 `consciousness_deep_dive_scan_pc.yaml` 和连续任务 `consciousness_deep_dive_planned_run_pc.yaml`。扫描默认`recognition_goal: targets`，保留`full`兼容选项。**连续任务尚有上述已知阻塞，应先修复及离线验证再恢复实机。**

## 暂停前验证与现场

全部相关文件收集执行显示 **530 passed /34.82秒**，但进程最终exit1、日志无测试失败；这个退出问题尚未调查，不能记为整体命令成功。最后新增补读扩展、流程及模型注入的65项定向回归通过/0.62秒、exit0。manifest sync/check/validate通过；plan_doctor零错误、136条Python缓存/字节码警告（此前131），没有清除用户已有缓存；`git diff --check`通过。

验证必须从仓库根运行，临时目录、截图和日志放仓库内。pytest使用专门`--basetemp=.pytest_tmp/<scope>`，不能清掉保存原图的整个`.pytest_tmp`。例如：

```powershell
$taskTmp = Join-Path (Get-Location) '.pytest_tmp/deep_dive_resume/os_tmp'
New-Item -ItemType Directory -Force $taskTmp | Out-Null
$env:TEMP=$taskTmp; $env:TMP=$taskTmp; $env:TMPDIR=$taskTmp; $env:PYTHONUTF8='1'
$testPaths = @(rg --files tests/unit | Where-Object { $_ -match 'test_deep_dive' })
.venv/Scripts/python.exe -m pytest @testPaths --basetemp=.pytest_tmp/deep_dive_resume/unit -q
.venv/Scripts/python.exe -m packages.aura_core.cli.package_cli sync plans/resonance_pc
.venv/Scripts/python.exe -m packages.aura_core.cli.package_cli check plans/resonance_pc
.venv/Scripts/python.exe -m packages.aura_core.cli.package_cli validate plans/resonance_pc
.venv/Scripts/python.exe tools/plan_doctor.py --plan resonance_pc
```

现场游戏保持打开，最后实际HUD：第一位面、六回合、移动0/1、旋转1/1、灵感0/2；本轮仅改变观察/操作视角，没有选择目的格、进入战斗或购买。最后截图被Codex部分遮挡，不能当完整HUD新核验；以最后任务同帧HUD记录为准。测试runner已关闭，遗留的本任务测试Python已停止。暂停后没有再运行游戏测试、没有开始新修复。
