# 模板复用优化验证记录

日期：2026-10-22。本记录覆盖通用视觉模板缓存、inspect 预览、角色与仓库素材复用、砍价同轮截图复用、收菜专用匹配及派遣分类点击调整。

本页保留首次验收时的源码和数据；单独房间判断、资源预检的后续路径优化及最新同窗三版本结果见 [收菜路径检查后续验证](collection-template-path-optimization.md)。

## 结论与边界

最终五组 pytest 共执行 1,733 项，按 `(classname, name)` 去重后为 **1,627 项，全部通过，无跳过**。新旧代码差分 **691 组，全部通过**。覆盖范围内的识别身份、坐标、优先级、阈值判定及异常类型、消息、操作顺序保持兼容；收菜浮点分数比较容差为 `1e-7`，其他确定性结果按精确值比较。

重复匹配和完整收菜观察有可测收益；单独房间判断及热态资源预检存在回退，详见下表。所有计时都是离线 CPU/文件读取耗时，不包含真实截图接口、游戏动画、输入或页面等待，不能换算为整项游戏任务的完成时间。

本轮未修改深潜专用代码或素材，也未作深潜实机验收。通用缓存会影响深潜使用普通模板路径的调用，相关保障来自通用 API 契约检查。

## 证据范围与协调

证据根目录为仓库内 [本轮验证目录](../../.pytest_tmp/template-match-task-optimization-6481f503739144e6aa899a48fdf88b42/)。执行前保存了 5,037 个已有脏工作区文件的哈希；本轮与其他授权对话的改动分别保留，没有回退或提交他方工作。

差分的旧实现来自本次优化前保存的 `.codex_tmp/template-match-optimization/before/`，不是当前 Git HEAD；之前的模板目录整理与元数据调整仍在该基线中。快照依赖只存在于本轮临时差分脚本中，新增正式单测不依赖该忽略目录。

与“分析模板匹配性能”对话明确分工：共享 `_reforming_center_room_templates.py` 归该对话维护，本侧收菜使用独立匹配模块。共享模块最终 SHA256 为 `867d18c8c0032e6a37abc8301836d699b1a46c1b520d737f27b5f05f799f3a1d`。该对话的 234 项测试、245 组差分与计时结果不计入本报告；其房间扫描总耗时尚无稳定改善证据。

与“梳理雷索纳斯任务层级”对话协调了普通匹配 adapter、导航和公开观察服务的修改边界。其早期插入本侧收菜的注入改动已由该对话移回独立 adapter；其后续 adapter 差分与异常修复属于独立范围，不计入本侧兼容结论。最终性能窗口串行执行，其他对话暂停重型图片差分。本侧最后仅修改文档。

[汇总 JSON](../../.pytest_tmp/template-match-task-optimization-6481f503739144e6aa899a48fdf88b42/final-summary.json) 保留原始报告与统计；[最终源码冻结清单](../../.pytest_tmp/template-match-task-optimization-6481f503739144e6aa899a48fdf88b42/final-source-freeze.json) 核对 14 个当前源码、配置、测试文件与最终计时记录的哈希一致。

## 测试与差分

| 分组 | 最终通过数 | 主要覆盖 | 结果文件 |
| --- | ---: | --- | --- |
| 通用缓存及视觉 API | 317 | 同步/异步 8 个匹配 API、预处理、mask、只读快照、LRU、并发、文件/链接版本失效、inspect | [core/pytest.xml](../../.pytest_tmp/template-match-task-optimization-6481f503739144e6aa899a48fdf88b42/core/pytest.xml) |
| 角色、仓库、砍价 | 164 | 头像加载、入口轮询、截图次数、取消、预算、未知状态和耗尽连续确认、原 RGB 对象兼容 | [tasks/report-fixed.xml](../../.pytest_tmp/template-match-task-optimization-6481f503739144e6aa899a48fdf88b42/tasks/report-fixed.xml) |
| 相邻调用方回归 | 1,127 | 便当、城市、出发、乱斗、库存、恢复、交易、回收、乘客、黑月、海角、货运与异步约束等 37 个文件 | [regression-results.xml](../../.pytest_tmp/template-match-task-optimization-6481f503739144e6aa899a48fdf88b42/regression-results.xml) |
| 收菜与派遣 | 36 | 导航身份、模态优先级、数字、ROI、阈值独立判定、快照与链接边界、动作配置 | [collection/pytest.xml](../../.pytest_tmp/template-match-task-optimization-6481f503739144e6aa899a48fdf88b42/collection/pytest.xml) |
| 集成补查 | 89 | 城市视觉及身份资源、异步安全、框架动作复用 | [integration-results.xml](../../.pytest_tmp/template-match-task-optimization-6481f503739144e6aa899a48fdf88b42/integration-results.xml) |

各组有重复用例，因此不把 1,733 当作独立测试数。

| 差分 | 数量 | 比较内容 | 结果文件 |
| --- | ---: | --- | --- |
| 通用视觉及 inspect | 409 | 冷/热调用返回全部字段；source/template/mask 缺失、损坏、非法索引/数量的异常类型、消息和先后顺序 | [core/differential.json](../../.pytest_tmp/template-match-task-optimization-6481f503739144e6aa899a48fdf88b42/core/differential.json) |
| 任务层 | 23 | 290 个真实头像的全部像素与元数据，RGB/灰度/RGBA 输入及 7 类加载错误，仓库读取/取消顺序、砍价状态和预算 | [tasks/differential-fixed.json](../../.pytest_tmp/template-match-task-optimization-6481f503739144e6aa899a48fdf88b42/tasks/differential-fixed.json) |
| 收菜 | 259 | 真实模板构造的客户端图与现有截图夹具；身份、优先级、编号、ROI、分数、坐标及文件/非法输入异常 | [collection/differential.json](../../.pytest_tmp/template-match-task-optimization-6481f503739144e6aa899a48fdf88b42/collection/differential.json) |

文件更新行为刻意比旧的永久 LRU 缓存更严格：新观察会检查版本、删除与链接目标变化；同一次收菜观察中首次使用后固定不可写模板及目录拓扑，下一次观察重新校验。差分比较同一版本素材的识别结果，不要求新代码复现旧缓存继续使用已删除或过期像素的行为。

验证过程中修复了三个兼容问题：inspect 在模板缺失或损坏时恢复原有空预览与源参数；默认观察恢复传入原 RGB 数组，仅显式共享帧才提前灰度化；收菜恢复旧读取器的缺失/损坏文件异常类型和消息。第一轮相关失败与后续修复结果均保留在证据目录，没有用重跑覆盖首次证据。

## 离线耗时

以下为单机配对、交替先后顺序的中位数，单位 ms。通用视觉为 25 对样本，任务层和收菜为 7 对；任务层轮询样本内循环 30 次，头像 catalog 每样本重新加载一次。原始 JSON 同时保存各样本、P95、IQR 和源码哈希。

“冷态”表示新服务或应用缓存清空后的调用，**未清操作系统磁盘缓存**。任务层表中的首次调用只有单次观测，不能当作稳定冷态分布。通用基准设 OpenCV 单线程，收菜记录为 32 线程；仅在各自配对场景内比较，不跨场景平均百分比。

### 通用缓存

素材为真实海角卡片、公共 mask 与派遣控件，源为对应任务大小的生成 ROI；不是整屏或实机截图。详见 [core/benchmark.json](../../.pytest_tmp/template-match-task-optimization-6481f503739144e6aa899a48fdf88b42/core/benchmark.json)。

| 场景 | 冷态旧→新 | 热态旧→新 | 热态耗时变化 |
| --- | ---: | ---: | ---: |
| 9 张卡片共用 mask | 17.923→17.444 | 15.738→11.007 | 降低 30.1% |
| 单个派遣控件 | 0.334→0.352 | 0.292→0.213 | 降低 27.3% |

单控件冷态增加约 0.019 ms；缓存收益来自后续复用。最初的逐次 `Path.resolve()` 曾使小图热态变慢，现改为词法绝对路径键与原路径 stat，保留链接重定向失效检查；初始计时保存在 `core/benchmark-initial.json`。

### 任务层

详见 [tasks/performance-combined.json](../../.pytest_tmp/template-match-task-optimization-6481f503739144e6aa899a48fdf88b42/tasks/performance-combined.json)。前四行固定相同的无缓存 OpenCV 依赖，隔离任务层修改；最后一行同时启用本轮框架缓存与同帧观察。

| 场景 | 热态旧→新 | 耗时变化 | 次数变化 |
| --- | ---: | ---: | --- |
| 290 人物 catalog 重新加载 | 291.061→185.584 | 降低 36.2% | 解码 585→293 |
| 角色入口连续 10 次识别 | 3.927→2.941 | 降低 25.1% | 轮询内读图 10→0，模板随 catalog 准备 |
| 仓库入口连续 10 次识别 | 3.602→2.563 | 降低 28.8% | 每次入仓读图 10→1 |
| 砍价双检查，仅任务层 | 1.155→1.256 | 增加 8.8% | 截图 2→1，读图仍 2→2 |
| 砍价双检查，框架与任务组合 | 0.728→0.358 | 降低 50.8% | 截图 2→1，热态读图 2→0 |

截图使用固定 ROI 数组拷贝；截图次数减少的真实接口收益尚未实测。砍价两帧耗尽确认仍保留，只将每轮两次截图减为一次。组合场景首次调用为 0.894→0.969 ms，热态服务在后续轮询间持续持有。新增组合场景在独立实例上先核对了完整观察字段与匹配请求一致。

### 收菜完整观察及控制场景

真实 catalog 图片嵌入确定性 1280×720 BGR 背景，并额外使用仓库内两张真实生产车间截图。冷态每次清空应用缓存，热态每次先预热一次；图像构造在计时外。详见 [collection/benchmark.json](../../.pytest_tmp/template-match-task-optimization-6481f503739144e6aa899a48fdf88b42/collection/benchmark.json)。

| 场景 | 冷态旧→新 | 热态旧→新 | 热态耗时变化 |
| --- | ---: | ---: | ---: |
| catalog JSON | 1.569→1.648 | 0.0008→0.0006 | 太小且波动，不作收益结论 |
| 全部模板预检 | 58.701→36.138 | 0.076→4.175 | 增加约 4.10 ms |
| 4 组导航卡片 | 378.543→351.437 | 332.769→335.386 | 增加 0.8%，无稳定提速结论 |
| 3 个房间单独判断 | 46.080→46.812 | 34.206→37.964 | 增加 11.0%，约 3.76 ms |
| 6 种完整收菜状态 | 95.838→61.554 | 108.096→67.193 | 降低 37.8% |
| 2 张已有截图完整状态 | 48.467→32.568 | 40.968→25.785 | 降低 37.1% |

热态预检的旧实现完全信任永久图片缓存，新实现每次预检检查文件版本和目录边界；额外约 4 ms 是新增新鲜度语义的成本。单独房间判断复用机会较少，仍承担这些检查，暂不声称其提速。

首轮收菜测量发现导航与房间判断分别回退约 22%/32%。本轮将根/父目录解析和资产固定限制在一次观察内，下一次观察继续重查；补充文件替换、删除、文件/目录链接重定向、越界和不同帧嵌套测试后，导航回退缩小至本次的 0.8%，房间仍有上述成本。初始负结果保存在 [collection/benchmark-initial.json](../../.pytest_tmp/template-match-task-optimization-6481f503739144e6aa899a48fdf88b42/collection/benchmark-initial.json) 与 `before-performance-revision/`。

## 重放入口

均从仓库根目录运行，使用 `.venv/Scripts/python.exe`，并将 `TEMP`、`TMP`、`TMPDIR` 指向本验证目录下独立子目录，设置 `PYTHONDONTWRITEBYTECODE=1`、`AURA_VISION_TIMING_SAMPLE_EVERY=0`。

- 相邻回归：验证目录内 `run_regression.ps1`，其 pytest 临时目录、缓存及报告均留在仓库内。
- 通用差分及计时：`core/core_compare.py` 默认运行差分，添加 `--benchmark --repeats 25` 运行计时，输出用 `--output` 指定。
- 任务层差分：`tasks/test_task_template_reuse_differential.py`；计时：`tasks/task_reuse_performance.py --samples 7 --iterations 30 --output <报告路径>`。
- 收菜：`collection/run_collection.ps1 -Mode pytest|differential|benchmark -Rounds 7`，包装器固定使用本仓库 venv 并设置本地临时目录。

新增正式契约测试为 `tests/unit/test_template_image_cache_contracts.py`、`tests/unit/test_task_template_reuse_contracts.py`、`tests/unit/test_product_collection_matching_contracts.py`。本轮未运行生成器、重新提取游戏素材或打包发布流程。
