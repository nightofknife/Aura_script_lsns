# 独立结算收尾任务

入口：`tasks:consciousness_deep_dive_cleanup_pc.yaml:consciousness_deep_dive_cleanup_pc`，方案 `resonance_pc`，无输入参数。

只负责从“探索中断”结算第一页或详情页返回活动首页。若启动时已经在活动首页，则确认后直接成功。不会调用开始下潜，不会运行魔方单局任务；旧单局任务仍停在结算页。

流程：结算标题+空白继续提示 → 点击空白 → 结算标题+独立确定按钮 → 点击确定 → 活动标题+开始下潜入口 → 成功停止。具体成绩、队员、道具和动态魔方画面不参与判断。

每次点击后至少1秒再检查同页并补点；每页合计最多3次。页面变化到下一阶段后才继续，未知页／无效帧不点击，30秒无进展停止并保存截图。连续两次识别首页才完成。不将按钮消失直接当成首页到达。

三段后续可通过 YAML `aura.run_task` 串联：现有进入任务 → 现有单局任务 → 本收尾任务。外层应检查子任务 `user_data.deep_dive_cleanup.success` 和 `page_state`，不能只凭框架执行状态判断成功。本次未创建循环任务。

返回：`deep_dive_cleanup` 下含 `success`、`status`、`page_state`、`reason`、`last_frame`、`trace`。成功页面标记为 `deep_dive_activity_home`。截图在 `logs/deep_dive_cleanup/<session>/`。

当前只覆盖用户提供的探索中断结算流程，其他通关结束样式尚未适配。离线测试覆盖三页截图、亮度扰动、相似确认按钮反例、两次点击闭环、重试上限、无效截图、从详情／首页恢复，以及完整动作回放；未实机运行。
