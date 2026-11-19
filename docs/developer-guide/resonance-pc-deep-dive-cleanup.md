# 独立结算收尾任务

入口：`tasks:consciousness_deep_dive_cleanup_pc.yaml:consciousness_deep_dive_cleanup_pc`，方案 `resonance_pc`，无输入参数。

从「探索完成」或「探索中断」的结算第一页或详情页返回活动首页。若启动时已经在活动首页，则确认后直接成功。

流程：结算标题+空白继续提示 → 点击空白 → 结算标题+独立确定按钮 → 点击确定 → 活动标题+开始下潜入口 → 成功停止。具体成绩、队员、道具和动态魔方画面不参与判断。

每次点击后至少1秒再检查同页并补点；每页合计最多3次。页面变化到下一阶段后才继续，未知页／无效帧不点击，30秒无进展停止并保存截图。连续两次识别首页才完成。不将按钮消失直接当成首页到达。

完整循环在规划单局确认游戏已到结算后调用本任务，并在 `success` 为真且 `page_state` 为 `deep_dive_activity_home` 时计入完成局数。

返回：`deep_dive_cleanup` 下含 `success`、`status`、`page_state`、`reason`、`last_frame`、`trace`。成功页面标记为 `deep_dive_activity_home`。截图在 `logs/deep_dive_cleanup/<session>/`。
