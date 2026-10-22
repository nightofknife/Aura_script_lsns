# Framework OCR

检测框裁图默认只做透视矫正，**不再根据高宽比自动旋转 90°**。
单次调用可用 `rotate_tall_crops: true` 恢复旧策略：透视裁图高/宽≥1.5 时顺时针旋转 90°。
默认 `false`，用于保留窄高单字和数字的原方向；不通过识别内容猜测是否需要旋转。

```yaml
recognition_only: false
rotate_tall_crops: false       # 默认；true 恢复旧的竖长裁图 90°旋转
use_textline_orientation: true # 独立的 0°/180°方向分类，仍默认开启
```

`rotate_tall_crops` 适用于全部 10 个 OCR 动作及服务的四个查找/识别方法，
等待动作经过 Observation 服务时同样透传；按调用生效，不修改共享引擎。
`recognition_only: true` 仍跳过检测、透视裁图及两种旋转处理。
关闭 90°旋转不关闭方向模型，也不改变启动模型加载和预热。
检测候选框和结果结构保持原状；文字、置信度及筛选后的数量可能变化。
需要旧裁图转向行为的调用可显式开启；本参数不能修复漏检或透视裁图范围不准确。
固定检测框、保留方向分类的数字对照和默认改变的影响见
[OCR tall-crop benchmark](./ocr-tall-crop-benchmark.md)。

OCR 动作和服务方法支持单次调用参数 `use_textline_orientation`，默认 `true`。
设置为 `false` 时保留文字检测、检测框裁图和文字识别，仅跳过 0°/180°文字行方向分类、
其预处理及分类结果触发的 180°翻转。例如：

```yaml
recognition_only: false
use_textline_orientation: false
```

参数适用于本文列出的全部 10 个 OCR 动作，以及 `OcrService.find_text`、
`find_all_text`、`recognize_text`、`recognize_all`，等待和点击动作会逐次透传。
参数按调用生效，不修改共享引擎、模型元数据或其他调用的行为。
`recognition_only: true` 仍优先跳过检测和方向分类，不受此参数影响。
模型元数据原本关闭方向分类或未加载分类会话时，`true` 不会强行启用。

这个参数不改变启动模型加载与逐阶段预热。检测框透视矫正保留；竖长裁图的
90°旋转由独立的 `rotate_tall_crops` 参数控制。返回结构、区域坐标偏移和点击定位规则保持原状；
倒置文字的识别文字、置信度和筛选后的返回数量仍可能改变。
检查模式记录请求参数；开启分阶段计时后，关闭分类调用的 `ori_*` 阶段为零。
对照方法和真实样本结果见 [OCR orientation benchmark](./ocr-orientation-benchmark.md)。

识别宽度和批次补零策略可以独立配置，默认保持旧识别宽度并启用自适应分批：

```yaml
ocr:
  recognition:
    width_mode: legacy       # legacy / adaptive
    min_width: 160           # adaptive 模式的候选最小宽度，向上按 32 对齐
    batch_mode: adaptive     # fixed / adaptive
    batch_size: 8            # 仅识别批次；不改变方向分类批次
    split_min_saving: 0.25   # adaptive 分批须至少减少 25% 输入面积
```

`width_mode: legacy` 保留元数据的基础宽度下限（当前为 320）；`adaptive` 按文字
实际宽高比选择更短的输入宽度，保持高度 48、文字等比缩放及最大宽度 3200 的策略。
固定宽度模型使用模型声明的宽度。`min_width` 必须为正整数且不大于模型最大宽度。

`batch_mode: fixed` 保留按长宽比排序后固定数量分批的策略；`adaptive` 在该策略上，
仅当拆批能节省足够输入面积时递归拆分长短混合的批次，不跨越最大批次大小。
等宽批次和固定输入宽度模型不会为补零收益而拆批。识别后恢复原始顺序。
未配置 `batch_size` 时沿用模型元数据中的批次大小，当前模型为 8。

两个开关可分别启用，也可以组合；全部恢复原行为只需设置 `width_mode: legacy`
和 `batch_mode: fixed`。这些选项在引擎初始化时读取，修改后需要重启运行时。
自适应宽度额外预热最小宽度、常见短行宽度 192 和基础宽度下的单条/最大批次。
减少补零可能改变识别文字、置信度及筛选后的结果数量，不能视为零准确率风险。
真实样本的四组对比见 [OCR recognition benchmark](./ocr-recognition-benchmark.md)。

可选分阶段计时默认关闭。需要诊断任务中的 OCR 调用时，在全局或计划配置中设置：

```yaml
ocr:
  profiling:
    enabled: true
```

开启后每次识别输出一条 `OCR timing` 日志，并在成功识别结果的
`debug_info.timing` 中附带毫秒计时；没有文字时仍会输出日志。
关闭时不读取计时时钟、不生成计时记录，原有文字、置信度和坐标行为保持一致。
计时收集器按请求隔离，不使用全局的“最近一次结果”。

阶段包括图像转换、检测预处理/推理/后处理、文字裁图、方向分类预处理/推理/后处理、
识别预处理/推理/CTC 解码、结果组装和结果解析。分批调用会累加到对应阶段。
各阶段互不重叠，`other_ms` 表示总耗时与阶段之和的差值。
服务日志的总耗时不含截图、模型初始化和等待 OCR 信号量的时间，包含工作线程调度；
基准工具直接调用已预热的引擎，另行排除线程调度，以观察稳定态算法开销。

真实样本阶段基准及结果见 [OCR stage benchmark](./ocr-stage-benchmark.md)。

框架启动时默认加载当前 OCR 模型包的全部启用模型，并在同一组会话上逐阶段预热。
检测、文字行方向分类和文字识别分别执行，预热不依赖空白图能否检出文字；
方向分类和识别覆盖单条及配置的批次大小。成功预热后重复调用 `warmup_engine`
不会重复推理；`self_check` 仍会重新执行各阶段用于诊断。

预热发生在实际执行任务的运行时进程内。完成前运行状态和 runner 的 `ready`
保持为 `false`，任务消费者及调度循环尚未运行；全部准备好后才开放任务执行。
模型文件缺失、加载或预热失败会向启动调用方抛出错误，启动超时也会按原有超时机制终止。
当前模型包只加载一组检测、方向分类、识别会话，任务执行时复用。

旧配置未包含 `services` 时同样默认启用。发布配置显式写出以下启动策略：

```yaml
services:
  ocr:
    preload_on_start: true
    warmup: true
    required: true
```

`required: true` 表示 OCR 准备失败则框架启动失败。其他可选服务仍按各自预加载配置处理。
`recognition_only` 仅控制本次识别步骤，不影响启动时模型加载和预热。

OCR 模型会话默认使用 8 个 ORT 算子内线程，并关闭线程池自旋。
可在全局配置或计划的 OCR 配置中覆盖，示例：

```yaml
ocr:
  session:
    intra_op_num_threads: 8
    allow_spinning: false
```

`intra_op_num_threads: 0` 恢复 ORT 自动选择线程数；
`allow_spinning: true` 开启自旋。两项可分别覆盖，未设置的项使用框架默认值。
自旋开关同时控制算子内和算子间线程池；这些设置在模型会话创建时生效，
修改后需要重新创建会话（通常重启应用）。其他 ONNX 服务保留各自默认策略。
本机对比见 [OCR CPU benchmark](./ocr-cpu-benchmark.md)。

框架 OCR 动作支持可选参数 `recognition_only`，默认值为 `false`。
设置为 `true` 时，将本次输入图像直接送入文字识别模型，跳过文字检测、
检测框裁剪与矫正、文字行方向分类。输入区域和方向由调用方处理。

支持此参数的动作：

- `find_text`、`recognize_all_text`、`get_text_in_region`
- `check_text_exists`、`assert_text_exists`、`assert_text_not_exists`、`assert_text_equals`
- `wait_for_text`、`wait_for_text_to_disappear`、`find_text_and_click`

在动作参数中添加：

```yaml
recognition_only: true
```

直接调用 `OcrService` 时，`recognize_text`、`recognize_all`、`find_text`、
`find_all_text` 同样支持该参数：

```python
result = ocr.recognize_text(source_image=line_image, recognition_only=True)
```

直接识别最多返回一个结果，文字匹配和置信度过滤仍按原有规则执行。
结果的 `rect`、`center_point` 和 `debug_info.polygon` 表示输入图像的范围及中心；
`debug_info.recognition_only` 为 `true`。动作层仍会为 `rect` 和 `center_point`
加上 `region` 的偏移，转换为窗口坐标，`debug_info.polygon` 保留图像内坐标。
`find_text_and_click` 匹配成功后点击输入区域的中心。

此参数只控制单次 OCR 的执行流程，模型会话由启动流程提前加载和预热。

默认文字检测将长边限制在模型元数据 `det_preprocess.resize_long` 指定的上限内，
当前默认模型为 960；小图不主动放大，宽高继续按 32 像素对齐。
例如，200×100 的输入默认变为 192×96，1280×720 默认变为 960×544。

需要放大时，在本次动作中显式指定检测目标长边：

```yaml
det_resize_long: 960
```

这会将 200×100 的输入放大到 960×480。参数默认为 `null`，表示使用默认上限策略；
显式值为正整数，覆盖本次检测的目标长边，不修改模型元数据或影响后续调用。
上述全部 OCR 动作和 `OcrService` 的四个识别/查找方法均支持此参数，
例如 `ocr.recognize_all(image, det_resize_long=960)`。
启用 `recognition_only` 时跳过检测，`det_resize_long` 不参与处理。

识别多行文本时，框架按图像长宽比分批，减少同一批次内的补零，
识别后恢复原顺序，维持文字、置信度和检测框的对应关系。
检测、方向分类和识别预处理直接填充连续的 `float32` 张量；
普通查找复用截图，检查模式另存调试快照。CTC 解码复用最大值索引取分数，
避免重复扫描完整类别输出。
