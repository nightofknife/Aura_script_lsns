# 识海深潜实体检测服务

服务 ID 为 `plans/resonance_pc/resonance_pc_deep_dive_entity_detector`，类为
`plans.resonance_pc.src.services.deep_dive_entity_detector_service.DeepDiveEntityDetectorService`。
由 `@service_info` 注册为单例，并依赖 `core/config`。新增服务后通过仓库
`package_cli sync plans/resonance_pc` 生成 manifest；行动层通过 `@requires_services` 注入。

服务懒加载 ONNX；同一实例的初始化、推理、轻量融合与关闭都由一个锁串行保护。
`close()` 释放模型和唯一缓存，后续调用可重新懒加载。它不接收任务节点或姿态，
不负责截图、拖动、格子归属或投票。调用方必须将返回内容绑定到同源截图和冻结姿态。

```python
targets = entity_detector.detect(image_rgb)
packet = entity_detector.detect_packet(image_rgb)
entity_detector.close()
```

输入必须为 `uint8`、`720×1280×3` 的 RGB 数组。输出 `targets` 使用全图坐标：
`kind` 为 `player`、`singularity`、`inspiration`；`point=[x,y]`；
`box=[x,y,width,height]`；另有 `anchor_type`、`confidence`、`confirmable`。
玩家头的 `anchor_type="head"`。弱候选和被 HUD/已确认粉色头否决的候选仍以
`confirmable=False` 保留遮挡框，不将它们升为正票，也不把推理失败变成空列表。

局部粉色圆需与模型的同一个球形头框相互支持，才能确认玩家。未获得支持的圆
保留 `evidence_role="mask_only"`，目标类别置信度最多 0.24；原始圆形证据强度另存为
`raw_circle_confidence`。同一棋子身体与球头的连通证据可拒绝身体假圆；
不会通过全局 top-1 或仅凭中心距离删除不同玩家候选。

`detect_packet` 另返回 `model_executed`、`coverage_valid`、`cached`、`coverage_roi`。
`coverage_valid=True` 仅表示本图 `[250,40,1000,650]` ROI 成功完成推理，
不承诺任何格子的完整可见性或实体召回率。每格负证据仍需调用方检查位置、
遮挡、视角与 UI。最多缓存一帧精确 RGB 内容，命中返回深拷贝；即使像素相同，
缓存命中也标为 `model_executed=False,cached=True`，保守禁止将其当作新执行的独立负票。
任何一处像素变化（包括 ROI 外）都使缓存失效。
解码候选达到 `max_det` 上限时，返回 `proposal_limit_reached=True` 与
`coverage_valid=False`，保留候选遮挡信息，禁止将被截断的候选集合用于独立负票。

## 配置

在 Aura 配置中设置以下路径；相对模型路径以仓库根目录解析，不取当前工作目录。
配置不引用 `.pytest_tmp`、下载路径或某台机器的 Python 环境。

```yaml
resonance_pc:
  deep_dive:
    entity_detector:
      model_path: plans/resonance_pc/data/models/deep_dive_entities.onnx
      execution_provider: auto # auto / cpu / dml / cuda
      dml_device_id: 0
      hybrid: true
      weak_confidence: 0.05
      confirm_confidence: 0.6
      nms_iou: 0.5
      max_det: 64
      session:
        intra_op_num_threads: 2
        inter_op_num_threads: 1
```

`auto` 优先 DirectML，其次复用核心 ONNX 的 CUDA/CPU 选择。DML 使用顺序执行、
关闭 memory pattern；设备索引可配置，默认 0，不硬编码研究机器的索引 1。
显式 `dml` 或 `cuda` 不可用会报错；`cpu` 明确只用 CPU。
`status()` 可检查真正加载的 provider、模型路径、模型执行次数、缓存次数和最近耗时。

弱候选下限 0.05 为遮挡用途；正票门槛保留 0.6，并禁止配置到 0.6 以下。
局部头证据保留既有粉色内圈 .83、浅色外圈 .14、12 个角区至少 8 个角区的门槛。
黄色孔洞拓扑曾误删真灵感，没有作为否决条件接入。真实灵感与基础图案可同时存在。

## 安装与模型契约

默认模型随方案保存于 `plans/resonance_pc/data/models/deep_dive_entities.onnx`，
克隆仓库后无需复制本机研究目录。相邻 `deep_dive_entities.json` 记录模型契约、
字节数与 SHA-256；`README.md` 提供校验命令。自定义路径仍可通过配置覆盖。

在用于运行 Aura 的同一个 Python 环境中安装 CPU 可选依赖：

```powershell
.venv/Scripts/python.exe -m pip install -r requirements/optional-vision-onnx-cpu.txt
```

Windows DirectML 可在专用生产环境安装 `numpy<2.4`、`opencv-python` 和
`onnxruntime-directml`，再使用该解释器运行 Aura。不要在同一环境同时安装
`onnxruntime`、`onnxruntime-gpu`、`onnxruntime-directml` 这些共享同一模块的发行包。
服务不安装依赖、不下载模型、不启动研究目录里的 worker，也不修改 OpenCV 全局线程设置。
CUDA 使用仓库 `requirements/optional-vision-onnx-cuda.txt` 与核心 ORT 的准备逻辑。

模型必须为静态 FP32 输入 `[1,3,640,640]`、原始输出 `[1,7,N]`、不内置 NMS 的
YOLO11 检测导出，ONNX `names` 元数据顺序严格为
`player_head,singularity,inspiration`。服务验证输入/输出和类别契约后才推理。
预处理采用整数 letterbox padding，与已验证研究模型一致；颜色保持 RGB，除以 255。
解码与类别内 NMS 用 NumPy，不依赖 Ultralytics 或 PyTorch。

当前生产环境 CPU 烟测（12 个已保存截图、OpenCV 4 线程、ORT 2 线程）平均
69.04 ms/帧、P95 77.36 ms，首次加载 0.34 秒；精确缓存命中约 1.34 ms。
这只说明服务在当前 CPU 环境可运行，不能代替完整扫描实机耗时或识别准确率验收。
