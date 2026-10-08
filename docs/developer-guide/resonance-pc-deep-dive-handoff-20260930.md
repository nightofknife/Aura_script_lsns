# 识海深潜规划自动运行交接

更新时间：2026-09-30，Asia/Shanghai。用户要求停止当前调试、推送分支，换地方继续。

## 分支与结果

- 分支：`codex/deep-dive-planned-run-20260930`。
- 仓库：`nightofknife/Aura_script_lsns`。
- 起点：`0075d2b1`，来自 `codex/profile-panel-template-confirmation`。
- 提交范围：识海深潜规划器、GUI、关卡内自动运行、扫描修正、数字模板、设计说明、统计结果及精简实机证据。
- 原工作区还有便当、城市旅行、玩家恢复改动；这些保留在本机，未混入本次识海深潜提交。

**源码已接入，实机只验证到一次移动及战斗领奖完成，完整关卡验收未完成。** 框架 `success` 仅表示正常返回，不等于通关。

## 游戏现场：接续前重新读取

最后一次运行器于 19:06:35 清理退出。现场保留在普通棋盘扫描停止后的自由转动视角，没有继续输入。

| 字段 | 最后确认值 |
|---|---|
| 位面 | 1 |
| 剩余回合 | 6 |
| 移动额度 | 1/1，已消耗 |
| 旋转额度 | 0/1，尚未消耗 |
| 灵感 | 0/2 |
| 完整回合 | 尚未完成 |
| 玩家 | 已实际移动到第四次扫描逻辑 U01；返回后 Reset 图顶部右侧 |
| 待处理事件 | 无，强敌战斗及领奖已完成 |
| 业务停止原因 | `scan_not_complete:glyph_anchor_support_lost:insufficient_centres` |

**不要重复已经完成的移动。** 坐标只在所属扫描 epoch 有效，不能复用 U01 或旧像素 `[748,313]` 在新现场直接点击。换机器后先读取实际 HUD、新截图并建立完整新布局。入口支持半回合，按实际剩余额度规划。

## 用户已确定的规则

1. 三阶 3×3；每次奇点行动后重新完整扫描实际玩家、奇点和灵感，再规划。
2. 扫描结束先 Reset 到普通宽视图，两帧稳定三面注册确认玩家，再点击移动或旋转。不能额外打开旋转模式作为默认参考。
3. 移动放大遮挡侧面：普通宽视图建立共同 Q，移动视图独立重拟合当帧顶部及相机，绑定当帧青色候选。不沿用旧像素或伪造当帧侧面匹配。
4. 回合数必须模板读取，禁止 OCR 回退；全流程减少 OCR。
5. 玩家节点、玩家主动 Boss 相遇、奇点主动相遇分别持有事件上下文。Boss 战胜利需要后续实际位面/结算证据，不能当作整局结束。
6. 休整缺角色暂停，不自动招募或复活；最终结算保留，不自行退出、清理或重开。
7. 默认扫描 60 秒、规划 30 秒、安全回合上限 100。不靠延长扫描隐藏姿态问题。
8. 默认不写或运行测试。此前用户明确授权实机及相关截图诊断；现在已经要求停工，接续按新的授权执行。
9. 所有测试、检查、截图、日志和临时文件留在仓库内，`TEMP/TMP/TMPDIR` 指向 `.pytest_tmp/<scope>`；使用 PowerShell 7。
10. 浏览器优先复用 Playwright；稳定 Edge 与 Edge Dev 会话、token 分开，不能打印或搬运凭据。

## 入口与源码

GUI：小任务 → 识海深潜 → 移动规划 / 关卡内自动运行。任务从当前棋盘或休整页启动，到实际结算停止。

```text
tasks:consciousness_deep_dive_plan_pc.yaml:consciousness_deep_dive_plan_pc
tasks:consciousness_deep_dive_planned_run_pc.yaml:consciousness_deep_dive_planned_run_pc
```

下列 action 文件均位于 `plans/resonance_pc/src/actions/`：

| 文件 | 职责 |
|---|---|
| `consciousness_deep_dive_planned_run_pc_actions.py` | 主循环、HUD 共识、完整扫描、Reset、宽参考、规划、输入前复核、事件、奇点、位面、结算 |
| `_deep_dive_directed_operation_flow.py` | 指定移动/旋转事务、模式切换、稳定帧、预览与撤销 |
| `_deep_dive_operation_frame.py` | `build_wide_reference_frame`、`build_operation_frame`、`bind_move`、`bind_rotation`、预览核对 |
| `_deep_dive_runtime_events.py` | owner 事件上下文，复用已有事件与战斗 |
| `_deep_dive_planned_run_vision.py` | 场景与 HUD、未知休整弹窗冻结、有限 OCR |
| `_deep_dive_hud_templates.py` | 原生回合 0–30 和数字对模板读取 |
| `consciousness_deep_dive_scan_pc_actions.py` | 完整扫描、新鲜截图、输入所有权/取消、`reset_layout_view` |
| `_deep_dive_layout_vision.py` | 跟踪、图案网格姿态修正、锚点续期、occupant 唯一确认 |
| `_deep_dive_scan_stream.py` | 几何/语义 owner、epoch 标签的旋转和平移修正传输 |
| `_deep_dive_movement_planner.py` | 两种策略与半回合公共接口 |
| `_deep_dive_chase_planner.py` / `_deep_dive_inspiration_planner.py` | 追击 / 灵感策略 |
| `_deep_dive_planner_rules.py` | 邻接、层旋转、状态与奇点概率规则 |

模板在 `plans/resonance_pc/templates/deep_dive_planned_run/`，来源说明已保存，无运行时字体/客户端资源依赖。GUI 接线位于 `packages/resonance_gui/{logic,bridge,main_window}.py` 和 `widgets/consciousness_deep_dive_panel.py`。

新阶段：`scan_turn → accept_scan → reset_wide → wide_reference → planning → operation`。宽参考要求同一 scan/map/玩家、较早 view epoch、实际 HUD 未改变。真实动作或重新扫描使参考失效。

输入有异步取消 shield/drain，鼠标按下后 finally 释放；点击前同帧重新读 HUD、重建映射、检查网格变化。不能绕过门禁。

## 已执行与当前阻断

第四次会话 `58ed3c778e994e45a27d754806a7d47a`，CID `760456813765201920`：默认 60 秒内 scan0001 完整 54/54 → Reset 普通宽视图两帧三面注册 → 规划 U01 → 移动模式当前投影核对 → 真实目标点击 → 强敌胜利 → 选奖确认 → 返回棋盘 → HUD 确认移动 1/1。事件已关闭、pending action 清空。随后 scan0002 约 16 秒、20 格时失去图案锚点暂停。Reset 截图镜头正常。

### 已写入且运行过的扫描修正

- 已知图案 ≥6、至少两个面各 ≥2、分布足够时，联合拟合旋转和平移。
- 相对 Reset 的 X/Y 约 ±12 像素、深度 ±3.5%，局部步长与残差门禁保留。
- 几何 owner 原子接收 body rotation + translation delta + source epoch，过期拒绝，接受后重建特征射线。
- 锚点失去两秒暂停融合，六秒阻断；不伪造 occupant，不降低唯一玩家门槛。

### 待实施诊断

scan0002 初期 frame9/15 有七个独立图案，残差中位数约 2.65/2.16 像素。现有七图案续期额外要求跨面或七个已知，现场单面七候选、六已知没能续期；两秒融合冻结可能妨碍新面学习。

后期 frame97/171 覆盖图确实明显错位，平移与深度达到边界，图案减少到一个/零。六秒最终阻断是有效保护，不能只延时。

待实现方案：七个独立新图案在原局部/绝对 bounds、分布和残差门禁内允许续期及联合姿态；依据实测拖动增益限制两个语义采样之间的角度。**停工时这两项尚未写入，不能当作已经修复。** 扫描子代理确认没有新增编辑或仍运行的自建进程。

## 继续顺序

1. 拉取分支，读本文、实机记录和 evidence 中的 Reset、frame9/97/171、失败 layout 的 streaming/diagnostics。
2. 先修 scanner 换位后锚定和采样速度；保留 54 格、唯一玩家、真实图案和 pose bounds。不回退到头部单点或把未识别放宽为空格。
3. 获得接续授权后读取新现场。若移动 1/1、旋转 0/1，只规划剩余旋转。禁止复用旧扫描/旧像素或重复移动。
4. 覆盖指定旋转箭头绑定、预览、必要时撤销、确认额度，再观察奇点行动与下一回合新扫描。
5. 再覆盖奇点主动相遇/Boss、休整不足角色暂停、位面切换、最终结算、取消释放资源。未经实际证据覆盖前保留未验收标记。

## 跨机器证据与文档

精简证据在 [evidence/deep-dive-20260930](evidence/deep-dive-20260930/README.md)：session/events、成功/失败布局、Reset 和关键扫描原图/覆盖图、模板记录、运行辅助脚本、模型分析源码/JSON。

JSON 内原始绝对路径仍指向本机 `D:\project\aura_s_lsns`，查看时按 evidence 目录映射。原始 logs 整套截图、大型 `.npz` 缓存未推送。这些证据不是运行时依赖，不能从旧证据直接恢复地图驱动游戏。

- [用法](resonance-pc-deep-dive-planned-run.md)
- [实机记录](resonance-pc-deep-dive-planned-run-live-20260930.md)
- [循环设计](resonance-pc-deep-dive-planned-run-design.md)
- [指定操作设计](resonance-pc-deep-dive-directed-operations-design.md)
- [规划器](resonance-pc-deep-dive-planner.md)
- [模型规则](resonance-pc-deep-dive-movement-planning.md)
- [统计结论](resonance-pc-deep-dive-planning-statistics.md)

统计是规则模型，不是实机收益。两枚灵感在 52 格均匀抽样是假设；无限回合全部拾取并会合的最优总体概率为 1325/1326，不能写 100%。层转映射和事件副作用仍需完整实机覆盖。

停工后未运行新的测试、编译、打包或游戏操作。没有创建 PR 或自动合并。
