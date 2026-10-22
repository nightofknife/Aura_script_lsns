# 收菜目录查询去重与验证

日期：2026-10-22。本轮对照上轮已验收的 matcher `20f03ca9…`，继续调查三个房间判断与原始缓存实现间的耗时差。最终源码为 `aba543e210f67171ce7625dfd6fa2217b9e2e654a9514950812aca15c8e6439b`。

## 重复工作的来源

本机 CPython 3.12 的 `ntpath.realpath()` 在成功解析普通路径后，会再次查询同一规范路径，验证移除 `\\?\` 前缀后结果是否相同。`Path.resolve()` 随后还执行一次 stat，处理非 strict 模式的环路错误。上一轮三个房间的剖析中，144 次目录解析对应 288 次底层规范路径查询。

此次只对**输入和规范输出都符合保守语法的短盘符路径**省去第二次规范路径查询，继续查询完整的原始路径，保留 cwd 查询和解析后的 stat。没有沿已缓存的祖先拼出新目录，也没有减少叶文件版本检查。

快速分支限制为 Windows CPython 3.12、原生且未覆写的 Path.resolve、长度小于 248 字符的绝对盘符形式；组件只接受 ASCII 字母、数字、下划线、点和短横线，并排除点目录、尾点/空格、DOS 设备名及其扩展名、8.3 别名等。输入和输出都检查，路径拼写取自原生规范查询。UNC、扩展命名空间、Unicode、空格、长路径、原生 API 缺失或失败等均回原 Path.resolve 流程。句法判定的有界缓存不含文件系统状态。

发布使用的 Python 版本由 [release-contract.json](../../packaging/release-contract.json) 指定为 3.12；其他解释器版本走原流程。未全局修改 pathlib、OpenCV 线程或 IPP 配置。

## 检测和异常契约

每个新逻辑目录仍完整查询其路径，已触达目录仍遵循现有观察快照规则；下一观察重新查询。文件版本键、叶文件 lstat、解码前后版本核对均保持原样。

非 strict stat 的错误处理与原实现一致：普通 OSError 按原规则处理，ELOOP / WinError 1921 转为同样的 RuntimeError，cwd 的对应错误也保留转换及异常链。失败探测退出 except 块后才回原解析流程，避免额外异常 context。

最终源码的一次非计时完整三房间调用审计如下，三个返回值均为 True。Path.stat 总数包含 Path.lstat，不应把两行相加。

| 调用 | 上轮 | 本轮 |
| --- | ---: | ---: |
| 底层规范路径查询 | 36 | **18** |
| Path.stat 总调用 | 51 | **51** |
| 其中叶文件 Path.lstat | 33 | **33** |
| cwd 查询 | 18 | **18** |

见 [最终调用次数](../../.pytest_tmp/template-directory-checks-20261022-cedd2aee/collection/final-call-counts.json)。这验证了省掉的是重复规范查询，而非文件检查。任意系统调用之间发生并发重命名的纳秒级调度结果不作为等价承诺；已有的首次使用快照、跨帧更新与新目录祖先重定向检测均有回归覆盖。

## 测试与差分

最终 **94 项 pytest 全部通过，无跳过；385 组差分全部通过**。

| 检查 | 数量 | 证据 |
| --- | ---: | --- |
| 图片及完整观察的新旧差分 | 259 | [collection/differential.json](../../.pytest_tmp/template-directory-checks-20261022-cedd2aee/collection/differential.json) |
| 路径、文件版本及异常字段差分 | 76 | [path_semantics_differential.json](../../.pytest_tmp/template-directory-checks-20261022-cedd2aee/path_semantics_differential.json) |
| 目录规范拼写、回退及完整异常链差分 | 50 | [directory_semantics_differential.json](../../.pytest_tmp/template-directory-checks-20261022-cedd2aee/directory_semantics_differential.json) |
| 定向契约测试 | 94 | [pytest.xml](../../.pytest_tmp/template-directory-checks-20261022-cedd2aee/collection/pytest.xml) |

覆盖大小写、空格/Unicode、普通及扩展长路径、尾点空格、ADS/NUL、设备名、非法类型、PathLike、缺失/损坏文件、文件充当父目录、native 不可用及复杂输出回退、cwd/stat 权限与环路错误。精确比较异常类型、消息、args、filename、errno、winerror、context、cause 等；图片差分沿用 `1e-7` 分数容差，其余确定字段精确比较。

真实 Windows junction 场景验证：同观察已访问路径保持快照；祖先重定向后，尚未访问的新目录查询看到新目标，越界按原规则拒绝；下一观察更新目标。文件跨帧替换、删除、重建也保持一致。临时 junction 已移除，测试输入均在仓库内。

初次扩展长路径夹具遇到宿主普通路径创建限制，仅将夹具创建改用扩展前缀；比较时仍同时检查普通及扩展两种输入。静态审查还补齐了 cwd 环路错误的转换；最终测试及差分包含该分支。

## 离线耗时

与“分析模板匹配性能”“梳理雷索纳斯任务层级”对话确认修改范围及 CPU 窗口。首轮三版本全场景串行测量后，针对房间总耗时的不稳定信号，另借不超过 20 秒窗口做热态重复均值确认；其间其他对话暂停 CPU 重任务。

最终确认对照：原始模板缓存实现、上轮 `20f03ca9…`、本轮最终实现；OpenCV 32 线程，15 组轮换先后顺序。每个房间样本是连续执行 8 次三房间判断的平均值，预检样本是连续执行 20 次的平均值；未刷新 OS 文件缓存，不含真实截图、动画、输入和等待。

单位 ms，中位数；P95 是这 15 个批内均值样本的 P95，并非实机单帧 P95。

| 场景 | 原始 | 上轮 | 本轮 | 比上轮降低 | 更快配对 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 三个房间完整判断 | 37.7487 | 42.9476 | **38.2493** | **10.9%** | 14/15 |
| 91 文件资源预检 | 0.0168 | 1.3001 | **0.9459** | **27.2%** | 15/15 |

三房间 P95 为 48.9448→44.9444 ms，预检 P95 为 1.4122→1.0110 ms。原始预检命中缓存后不再检查文件变化，因此不能用该速度作为保留新鲜度检查时的目标。

首轮单次样本的候选计时也完整保留：预检热态 1.7074→1.3714 ms，14/15 更快；房间热态 41.3124→41.9003 ms，仅 7/15 更快，**没有显示稳定整轮收益**。导航等控制场景的原始正负结果均保留，未据此宣称全场景提速。后续重复均值结果支持连续热态下的收益，实机单次轮询仍需另外测量。

首轮候选为 `5a32b110…`，发生在补齐 cwd 环路异常分支前；正常输入快速路径相同。最终热态确认、94 项测试、385 组差分和最终调用次数均对应最终 `aba543e2…`。两次计时各有独立源码哈希，不混用为同一冻结版本。

- [最终热态均值及全部样本](../../.pytest_tmp/template-directory-checks-20261022-cedd2aee/collection/warm-confirm.json)
- [首轮候选三版本全场景计时](../../.pytest_tmp/template-directory-checks-20261022-cedd2aee/collection/final-benchmark.json)
- [首轮候选阶段剖析](../../.pytest_tmp/template-directory-checks-20261022-cedd2aee/collection/physical-attribution.json)
- [最终源码冻结及结果汇总](../../.pytest_tmp/template-directory-checks-20261022-cedd2aee/final-summary.json)

## 范围与重放

只改收菜 matcher、专用测试及本侧文档；共享房间 matcher、框架 adapter、VisionService、收菜 UI 与本轮开始快照一致，其他对话修改独立保留。未改深潜专用代码及素材，没有提交或打包。

从仓库根目录使用 `.venv/Scripts/python.exe`。本轮目录为 `.pytest_tmp/template-directory-checks-20261022-cedd2aee/`：

- `collection/run_collection.ps1 -Mode pytest` / `-Mode differential`。
- `path_semantics_differential.py`、`directory_semantics_differential.py` 重放扩展差分。
- `collection/warm_confirm.py` 重放最终热态确认；`collection/final_call_counts.py` 仅核对调用次数、不计时。

pytest 包装器设置仓库内 TEMP/TMP/TMPDIR、日志与 pytest 缓存。独立脚本同样需先把这些环境变量指向本轮目录，并设置 `PYTHONDONTWRITEBYTECODE=1`、`AURA_VISION_TIMING_SAMPLE_EVERY=0`；计时需与其他对话串行协调。
