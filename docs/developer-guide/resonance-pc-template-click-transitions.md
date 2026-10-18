# PC 模板识别与补点

本轮整理只涉及黑月准备/退出的公共执行骨架，以及到站、垃圾回收的失败识别和有界等待。深潜、框架、模板和历史截图保持原状。

## 公共执行骨架

`plans/resonance_pc/src/actions/_template_click_transition.py` 是 PC 包内部工具，不注册新 action 或 service。当前由 `black_moon_local_prepare_pc_actions.py` 和 `black_moon_local_exit_pc_actions.py` 使用。

- `TransitionProbe` 表达已确认的源状态、目标状态或未知状态。未知会清空连续确认，不能授权输入或成功。
- `wait_for_transition_probe` 为每次探测分配独立等待预算，通常要求两次等价观察。页面身份、坐标及视口是否稳定由场景提供。
- `run_template_transition` 在每次输入前重新探测并定位源按钮。若目标已到达则直接完成；源无法确认时停止。每次点击后先等待目标证据，再决定是否允许补点。
- 计时、异步等待和取消检查由调用方传入；原错误类型和结构化结果由场景构造，取消和技术错误不会被公共层吞掉。

| 参数或证据 | 黑月准备 | 黑月退出 |
| --- | --- | --- |
| 每次源/目标探测预算 | 7 秒 | 7 秒 |
| 轮询间隔 | 0.2 秒 | 0.2 秒 |
| 点击后等待 | 0.35 秒 | 0.5 秒 |
| 点击上限 | 首次加 3 次补点 | 首次加 3 次补点 |
| 稳定条件 | 页面、LOCAL/批量状态及按钮坐标 | 源按钮坐标及视口稳定；目标按目的地连续确认 |
| 特例 | 初次进入商店由已保证的休息菜单授权，入口单次命中即可；verify_ready 不点击 | 必须先允许退出；已到目标页面不点击 |

7 秒是每次探测的预算，不是整段操作的总预算。补点重新定位，不复用上次坐标。准备的临时识别歧义继续等待，模板损坏等技术错误继续向外传播。购买结算仍使用单独的 30 秒只读等待，未接入自动补点器。

## 到站与回收的技术失败

到站仅在自身调用中启用 `require_valid_capture`。截图失败、截图异常、空图、尺寸/类型错误和无效匹配分别报告 `arrival_capture_failed` 或 `arrival_template_match_failed`。其他旅行识别调用保留原默认方式。正常到站仍需点击后的两次有效未命中，单次未命中或技术失败不能确认到站。

回收所有截图必须有效，匹配结果必须有明确布尔状态且不含技术错误；失败分别报告 `rubbish_capture_failed` 或 `rubbish_template_matching_failed`。入口仍最多 3 次点击，回收按钮仍最多 5 次；正常有效消失的成功规则不变。

奖励关闭保留每次点击前 1 秒等待，增加最多 10 次点击和 15 秒预算。任一边界耗尽且未确认空状态，报告 `rubbish_reward_dismiss_timeout`，不继续盲目点击或返回。阻塞等待最长每 50ms 检查取消，`asyncio.CancelledError` 原样传播；取消或技术失败后不继续执行后续输入。

## 离线验证

测试覆盖有效/未知/错误观察、连续确认重置、原位置变化、独立等待预算、最大点击次数、取消、购买只读等待，以及回收完整 empty/sold 路径。

从仓库根目录运行，并使用独立的 pytest 临时子目录：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_template_click_transition.py tests/unit/test_black_moon_transition_contracts.py tests/unit/test_black_moon_local_prepare_pc.py tests/unit/test_black_moon_local_exit_pc.py tests/unit/test_arrival_template_confirmation.py tests/unit/test_rubbish_recycling_transitions.py --basetemp=.pytest_tmp/pc-click-transitions-tests
```

其他工具的 TEMP/TMP/TMPDIR 同样限定在仓库 `.pytest_tmp` 子目录。离线假服务和截图回放验证不代表当前游戏实机验收。
