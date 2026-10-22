# 公共观测服务

`plans/aura_base/observation` 为 Python 业务操作和已有等待 action 提供同一套观测实现。
识别内核继续使用 `vision`、`ocr`，截图继续使用注入的 `app`。服务不保存当前任务、当前帧或当前预算。

## 结果与坐标

- `Frame` 是只读像素快照，保存帧 ID、时间、目标身份、客户区 ROI 和明确的 RGB/BGR/GRAY 色彩空间。
- `Observation.state` 为 `present`、`absent`、`unknown`。有效未命中才是 `absent`；截图、模板或识别失败为 `unknown`。
- `Observation.value` 保留原有 `MatchResult` / `OcrResult`，坐标是客户区坐标。
- `Frame.crop()` 接收客户区绝对 ROI；局部坐标只在观察结果边界偏移一次。
- `WaitResult.matched` 表示在预算内满足稳定接受条件；最后一次观察保存在 `observation`，原始识别结果可由 `value` 读取。

`capture` / `capture_async` 取得新帧。`observe` / `observe_async` 接受单个探针、探针列表或按名称组织的探针，在同一帧上执行。
各探针返回 `Observation`，单个探针失败不将其他探针变成阴性结果。
已取得的 BGR 工坊图像可通过 `Frame.from_image(..., color_space="BGR")` 显式适配。

## 识别与等待

`match_template` 和 `match_templates` 接受已解析的模板路径、数组或 `PreparedTemplate`。
模板集采用同帧批量匹配；模板和掩膜必须适配当前 ROI。
`find_text` 支持候选列表、匹配模式、归一化、最低置信度，以及原有 OCR 识别策略参数。
模板路径仍由计划的既有模板解析器解析；服务不猜测当前游戏或模板目录。

`wait` / `wait_async` 统一支持连续帧数、语义键、中心容差、轮询间隔和最多探测次数。
未知观察清空连续稳定计数；同一帧不能重复计数；超时后的观察不能使等待成功。
业务可根据同帧证据决定页面含义；服务本身不判断购买、恢复或生产是否成功。

已有 `wait_for_image`、`wait_for_text`、模板集出现/消失、文字消失 action 委托本服务，保留公开参数及返回外观。
`timeout=0` 表示一次探测、不轮询；仍继承外层 operation 的预算和取消。
空模板集现在明确失败；连续截图或识别失败不再使“等待消失”返回成功。
查找后点击及普通转场已在后续批次接入[公共交互服务](interaction-service.md)；本文记录的是首批观察服务边界。

## 操作预算与后台工作

`packages.aura_core.scheduler.operation_scope.OperationScope` 为每次操作持有总截止和停止 token。
`child()` 与父操作共用停止状态，截止只能缩短；`checkpoint()` 先处理取消，再处理超时。
`timeout=None` 不新增局部截止，`timeout=0` 的 scope 则立即到期；一次探测适配由等待入口处理。

同步调用使用同步服务方法。异步调用通过 `scope.run_async()` 使用已有受追踪线程执行机制，
不为每个探针创建事件循环或线程池。取消后先禁止该 scope 的新操作，再等待在飞工作收尾。
同步截图或 OCR 不能强杀；超过 `execution.sync_action_cancel_grace_sec` 后，目标保持隔离，
直至实际 worker 结束。运行时拒绝隔离目标的新操作，按键/鼠标释放仍作用于已有会话。
状态查询中的 pending activity 继续记录实际在飞工作，取消请求不意味着工作已完成。

目标隔离以当前运行时目标实例为界，保护共享该实例的调用；不宣称跨进程或跨独立运行时实例的全局窗口互斥。
本批提供自动解除实际在飞租约的机制，不新增 GUI 人工解锁按钮，也不终止用户游戏。

## 首批迁移范围

工坊样板仅迁普通房间模板匹配，使用显式注入的 `VisionService`。
保留客户区 1280×720、BGR/灰度处理、阈值、房间身份、地图和颜色判断；不修改点击、补点或消费规则。
共享房间匹配器及独立收菜缓存由并行性能工作维护，本批以独立框架适配器接入房间导航。

本批已按明确授权完成离线测试与新旧差分，修复资源加载异常兼容、模板解析总预算和取消 drain 收尾竞态；详细结果与 NaN/Inf 的明确契约差异见[首批验证记录](observation-batch1-validation.md)。本次未进行实机验收、性能基准或打包。
