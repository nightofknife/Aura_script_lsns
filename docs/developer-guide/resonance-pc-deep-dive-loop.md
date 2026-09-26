# 识海深潜完整循环

入口：`tasks:consciousness_deep_dive_loop_pc.yaml:consciousness_deep_dive_loop_pc`，方案 `resonance_pc`。

唯一输入 `loop_count`：默认1；正整数表示完整局数；-1持续循环，直到用户取消或某段失败；0、小于-1、小数、布尔值无效。GUI的小任务→识海深潜增加“循环次数（-1持续循环）”和“开始完整循环”，旧入口与魔方测试按钮保留。

启动时必须在识海深潜活动首页，通过标题与开始下潜连续两次检查。不从结算页或魔方中途猜测恢复，不发送额外退出点击。

## 三段顺序与计数

外层 `aura.run_task` 调用逐局编排子任务，每局依次：

1. `consciousness_deep_dive_pc`：要求 enter_stage 输出 success=true、status=completed、page_state=deep_dive_board。
2. `consciousness_deep_dive_single_run_test_pc`：要求 finish 输出 success=true、status=completed、terminal=settlement。游戏结果failure（探索中断）仍是有效的单局终点。
3. `consciousness_deep_dive_cleanup_pc`：要求 finish 输出 success=true、status=completed、page_state=deep_dive_activity_home。

只有第3段确认成功才 completed_runs +1。子任务内的单局安全回合上限保持20，与局数无关。达到目标后停在活动首页，不点击下一次开始下潜。

`aura.run_task` 返回的是 framework_data，检查点读取节点业务输出（entry的enter_stage、其他段的finish），不误用框架SUCCESS代替业务完成。阶段异常标记blocked并跳过后续子任务；框架异常直接终止父任务。结束返回 deep_dive_loop，含 success/status/stage/loop_count/completed_runs/run_index/reason/last_frame/last_result。

## 长时间运行与取消

引擎while循环新增显式 max_iterations=-1（无限）和 retain_last=1（只保留最后一次输出）。现有任务默认1000及保留全部结果不变。循环每次迭代响应取消并让出事件循环。外层及三次run_task节点使用timeout=-1，GUI派发timeout=0，不受GUI单次等待超时截断；各子任务本身的页面、点击和安全限制不变。

每局单局任务创建新会话，正常结束时自行释放；外层检查收尾完成后释放本次收尾会话，外层结束释放循环会话。外层只保存计数和最近一次简要结果；落盘历史记录仍由框架保留。

进度事件 `task.resonance_pc_deep_dive_loop_progress`，schema= `resonance_pc.deep_dive_loop.v1`，携带外层cid和递增sequence。GUI按cid/任务/schema/sequence过滤，显示当前局、阶段和已完成局数。取消不会自动收尾或关闭游戏，当前未完整收尾的一局不计数。

## 验证

离线测试覆盖真实DAG两局串联、三段失败短路、无限循环超过1000次、保留最近输出、异步取消、起始页检查、GUI参数/进度/无总超时派发，以及原有单局与收尾回归。三段游戏操作未在本次实现期间实机运行，下一步需分别验证有限循环和持续循环的取消。
