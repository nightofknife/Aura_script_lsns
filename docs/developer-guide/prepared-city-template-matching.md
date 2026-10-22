# 城市模板的准备结果复用

## 范围与接口

第二批只在城市徽章识别中启用 `PreparedTemplate`。商品识别、城市入口按钮、改造中心及其他业务继续使用原有输入；不引入文件缓存、跨动作帧复用或持久匹配线程池。

`PreparedTemplate` 定义在 `plans/aura_base/src/services/prepared_template.py`，也可以从 `vision_service` 导入。四类模板 API 的同步、异步入口都支持它：

```python
import cv2

from plans.aura_base.src.services.vision_service import PreparedTemplate

templates = [
    PreparedTemplate(rgb_image, mask, use_grayscale=True, preprocess="edge")
]
hits = vision.find_templates_batch(
    source_image=current_rgb_roi,
    template_images=templates,
    mask_images=None,
    threshold=0.55,
    use_grayscale=True,
    match_method=cv2.TM_CCORR_NORMED,
    preprocess="edge",
)
```

构造只接受 ndarray 模板和可选 ndarray 蒙版，取得独立、由不可变 bytes 支撑的像素快照。公开的 `template_image` / `mask_image` 是这些原始快照；设置可写标记不能重新获得写入权限。`use_grayscale` / `preprocess` 表示固定的准备参数，`is_prepared` 表示成功发布了准备结果。

## 延迟准备与异常

构造时不执行灰度、Canny 或其他预处理。首次匹配仍先准备当前 source，再在既有请求的异常边界内准备模板和蒙版；后续请求仅复用模板侧结果。每次 source 都重新按同一管线处理，不对整帧预处理后再裁剪，也不二次处理已准备模板。

准备失败不发布半成品、不缓存异常，下次请求可以重试。并发首次访问由对象锁保护，成功后以完整 tuple 发布；已准备对象的读取不再取锁。模板匹配方法、阈值和分数计算不属于准备参数。

请求的灰度及预处理参数必须与对象一致；对象已封装蒙版，不能再传非 None 的外部蒙版。这些冲突按原 API 的 `ValueError` 处理约定返回失败结构。batch 的蒙版数量不符仍直接抛出 `ValueError`。原始 ndarray / 字符串路径输入保持既有准备顺序，包括先处理 source、再模板、再蒙版；`cv2.error` 等未捕获异常继续传播。

模板尺寸或蒙版尺寸不合法时，仍在原来的准备或匹配位置失败。`is_prepared=True` 不代表一次匹配成功，也不代表接受阈值；合法准备后发生的匹配失败不会销毁准备结果。

## 城市目录生命周期

`_city_panel_vision.load_templates()` 保留原来的四项目录解包接口，返回 tuple 子类，并额外持有 `prepared_templates`。这 21 个对象只用于城市徽章匹配，封装原来的 gray + edge 模板准备和灰度蒙版。

目录仍由原 `lru_cache(maxsize=1)` 持有；调用 `load_templates.cache_clear()` 后，下次加载会创建新快照和新的准备状态。它不监控磁盘文件变动，也不按 ndarray 的 id 建全局缓存。重载前的进行中请求可以继续持有自己的旧目录。

## 阶段计时与验证边界

第一批的 `AURA_VISION_TIMING_SAMPLE_EVERY` 开关继续有效，模板请求增加 `prepared_template_count` 字段。冷请求记录模板准备阶段；暖请求跳过模板和蒙版准备，因此相应 stage 字段可以缺失。source 准备和匹配计算仍每次记录。

验收使用实施前工作区的源码快照作为 oracle，而不是假定 Git HEAD 已含第一批。新旧对照包括全部结果字段、异常和顺序；城市的 `elapsed_ms` 属于耗时数据，不要求相等。有限置信度和像素转换使用精确比较。

耗时对比将目录加载及对象构造、首次延迟准备、重复识别分别报告，交替先后顺序采样。城市离线 probe 使用内存中的真实截图 ROI，不包含真实 WGC 等待、输入操作或游戏延迟；不能把它的比例当作完整任务提速。
