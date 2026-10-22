# 模板匹配执行与阶段计时

## 第一批改动范围

`plans/aura_base/src/services/vision_service.py` 的四类模板 API 共用完整同步请求内核：

- `find_template` / `find_template_async`
- `find_all_templates` / `find_all_templates_async`
- `find_templates_batch` / `find_templates_batch_async`
- `find_all_templates_batch` / `find_all_templates_batch_async`

同步入口直接在调用线程准备图像、匹配及包装结果，不再回到 scheduler 事件循环后向默认线程池二次提交工作。普通同步线程调用不再要求存在 scheduler loop。原先对 scheduler 事件循环线程调用同步接口的拒绝行为继续保留。

异步入口通过一次 `asyncio.to_thread` 执行完整请求，包括文件读取、解码和预处理。批量请求继续只准备一次源图，保留原有小批量串行和大批量临时线程池、`executor.map` 输入顺序。

预处理、颜色约定、蒙版、匹配方法、阈值、分数归一化、NMS、结果排序及 `debug_info` 不变。四类请求仍只捕获 `FileNotFoundError`、`TypeError`、`ValueError`，并返回各自原有失败结果；batch 蒙版数量不符仍向外抛出 `ValueError`。`cv2.error` 等其他异常继续传播。

本批未增加模板缓存、PreparedTemplate、持久匹配线程池或跨动作帧复用，也未修改 WGC 及业务私有匹配实现。`find_color` 保留原有桥接方式。

## 取消与图像所有权

本次改动不提供强制中断 OpenCV 计算的能力。异步调用方取消等待后，已经开始的 worker 仍可能运行至结束；同步 Action 继续由现有取消标记和宽限等待处理。同步模板调用改为直接计算后，调用线程要等计算返回，不再依赖桥接协程取消时提前解除等待的行为。

`vision_actions.find_image` 普通模式借用 capture 的独立图像，匹配路径不原地修改该源图；inspect 模式仍复制图像保留调试快照。后台 WGC 会原地更新共享帧，锁内快照复制仍保留。

## 启用采样计时

在启动应用的进程环境中设置 `AURA_VISION_TIMING_SAMPLE_EVERY`：

| 值 | 行为 |
| --- | --- |
| 未设置、`0`、负数或无效整数 | 关闭 |
| `1` | 记录每次调用 |
| `N > 1` | 每个 operation 分别记录第 1、1+N、1+2N 次调用 |

变量在私有计时模块首次导入时读取，变更后需要重启应用。计数器在同一进程内共享；同一个模板 API 的 sync / async 调用共用计数，不同 API 分别计数。`template_capture` 有自己的计数，由六个基础模板动作共享。

关闭时不创建请求计时器、不读取阶段时钟，也不记录事件。启用采样会引入时钟、统计锁和日志开销，尤其是 `N=1`，不应将其作为无开销运行模式。

实际开始执行的采样请求在工作结束时写入一条既有 RuntimeTrace `vision.timing` 事件，可在应用日志中搜索该事件名。使用既有 cid / activity 关联调用，不保存源图、模板像素或文件路径，不改写匹配结果的 `debug_info`。

## 事件字段

| 字段 | 含义 |
| --- | --- |
| `operation` | 四类模板 API 名或 `template_capture` |
| `execution` | 模板请求的 `sync` / `async` |
| `queue_ms` | async 从准备提交到 worker 开始的等待；sync 和捕获为 0 |
| `work_ms` | worker 或同步请求开始至结束的墙钟时间 |
| `total_ms` | 提交至工作结束的时间，包含 async 排队 |
| `stage_ms.source_prepare` | 源图准备耗时，batch 仍只执行一次 |
| `stage_ms.template_prepare_sum` | 模板准备耗时合计 |
| `stage_ms.mask_prepare_sum` | 蒙版准备耗时合计 |
| `stage_ms.match_sum` | 所有 `cv2.matchTemplate` 调用耗时之和 |
| `stage_ms.postprocess_sum` | 已完成的匹配后处理耗时之和，包含 all 的候选构造和 NMS |
| `stage_ms.batch_compute_wall` | batch 准备结束后，匹配调度、计算、结果收集及临时池关闭的墙钟时间 |
| `stage_calls` | 各阶段记录次数 |
| `candidate_count` | all 请求过阈值像素候选数，batch 为合计；best 请求为 0 |
| `source_kind` / `source_shape` | 源图输入类型、数组形状；路径输入不额外解码以获取尺寸 |
| `template_count` / `path_template_count` | 列表/元组输入的模板总数、字符串路径模板数 |
| `use_grayscale` / `match_method` / `preprocess` | 当前准备及匹配配置 |
| `status` / `error_type` | 请求执行状态、异常类型，不包含异常正文 |

阶段缺失表示没有执行或没有完成该阶段。并行 batch 的 `*_sum` 可能大于请求墙钟时间，各阶段不能直接相加作为占比；`batch_compute_wall` 与其包含的 match / postprocess 也不能重复相加。

`completed` 表示调用正常返回，包括未识别到目标；`handled_error` 表示按既有契约将错误转换为失败结果；`failed` 表示异常继续向外传播。计时记录失败不覆盖原结果或异常。

`total_ms` 表示工作完成时间，不代表已取消的异步调用方等待了同样长的时间。排队后尚未开始的工作若被取消，可能不会有完成事件。

## 截图计时边界

基础 `vision_actions` 的六个截图入口单独记录 `template_capture`，附带 `action` 和 `region`。其 `work_ms` 为 `app.capture` 调用耗时，不包含后续路径解析或匹配。capture 返回 `success=False` 仍表示调用正常返回，保留原动作的失败处理；捕获异常标为 `failed` 并原样抛出。

业务直接调用 `app.capture`、直接调用 `cv2.matchTemplate` 的路径不自动覆盖。城市识别等业务已有的总耗时仍可保留，但不能直接当作纯匹配耗时。首批实现没有运行测试、基准或实机性能采样，尚无性能提升比例结论。
