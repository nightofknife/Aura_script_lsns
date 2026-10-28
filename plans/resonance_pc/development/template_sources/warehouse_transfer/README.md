# 成品仓取出：模板来源与离线校正

`manifest.json` 记录用户提供截图的原始附件名、SHA-256 和状态。`screenshots/` 保留四张早期原图和 2026-10-24 新增的 `warehouse_stable.png`。前四张为 1282×752，裁 `[1,31,1281,751]`（XYXY）；新增稳定截图裁 `[26,56,1306,776]`，均得到 1280×720 客户区，不缩放。所有标注坐标均相对客户区。

2026-10-24 按稳定截图将仓库页与目标连接识别拆开：三个固定标题使用灰度结构匹配；目标按钮先定位结构，再在按钮内部核对彩色状态，保留旧未选中外观为独立变体。旧标题亮／暗模板仅作诊断，未选中不推断另一组连接。控件生成器默认读取这五张供图，写出带来源与裁剪记录的素材及实际识别器回放报告；本次五张供图回放及 19 项针对性测试通过，另回放首次失败实机的 13 帧。首次实机没有取货；修复后另一次授权实机（CID `769185061948887040`）完成涤纶 20 装车、只读复扫及返回总览，载货 56 → 76。该单商品、单视口结果不代表其他分支或服务器回读已验收；详细证据与范围见开发指南中的取货方案文末。

**当前数量方案为 OCR。** 入口是 `_prison_warehouse_quantity.py::WarehouseQuantityReader`，配置是 `data/meta/prison_warehouse_quantity_ocr_pc.json`（均相对 `plans/resonance_pc/`）。正常完整格子先裁 `[7,65,76,16]`；仅在文本不符合完整正整数或分数低于 0.80 时，再固定裁 `[7,66,76,15]` 识别一次。横向保留完整 76px，不按样本最多两位压缩宽度；不设未经游戏证据支持的四位上限。

本轮排除人工破损样本，经正式 reader 与真实现有 OCR 引擎离线核对：亮态 47/47、暗态 25/25，共 72 个正常样本全部读对、0 拒识、0 错误接受，仅一格使用备用区域，共 73 次 OCR。裁剪参数在这批供图上校正，不是独立实机准确率。当前持久摘要见 `quantity_ocr_calibration.json`；下面原生模板与早期 OCR 结果均为历史对照。

模板目录分别为 `templates/workshop/warehouse/controls/`、`goods/`、`native_digits/`，均相对 `plans/resonance_pc/`；旧 `digits/` 保留作截图模板对照。原生数量目录是 `data/meta/prison_warehouse_native_digits_pc.json`，其余目录与裁片参数在 `data/meta/prison_warehouse_*_pc.json`。返回键复用 `GLOBAL_BACK`，不重复生成；不制作最大模式检测模板。

控件取自供图。商品图标从本机游戏原生透明 sprite 提取，不使用交易所背景裁片；84×84 商品格内只取 `[8,24,68,42]`，另存前景掩码，避开角标、数量和底部白条。截图中的绿色布料为 ID113「涤纶」。生成成功不等于已校正：商品目录逐项记录支持状态、截图覆盖及已观察的区域。

数量必须保留字形附近的黑色描边。底部白条不能直接当作数字前景，也不能整行裁掉，以免切断进入白条的描边。数量识别单独校正，不复用背包页的数字区域和阈值。低置信度、存在候选歧义、未支持字形或裁切不完整应返回未知，不能当作 0，也不能据此判断库存达标。

前一版截图数量素材包含 0～7 的实拍单字模板（2 有两种相位），8／9 仅有原生字体生成的未校正候选，不进入旧 reader。47 个亮态样本读对 46 个、拒识 1 个、错误接受 0 个；25 个暗态列车样本全部拒识。72 个样本中 8 个用于建库，其余样本也参与门限校正，不是独立准确率。白条及破损合成负例 7 个目前均拒识，但任意破损、8／9 和未见背景仍存在误认风险；`auto_click_ready=false`。这套素材保留为对照，按用户要求另从原生 TTF 重建完整 0～9。

原生字体 `HarmonyOS_Sans_Bold` 内嵌于 `Patch/Asset/ui/font.asset`，字体 PathID 为 `-4884400286118422667`。它没有静态 `CharacterRects`，绑定纹理为 0×0，因而不存在可直接裁取的数字 atlas；必须区分原生 TTF 字形重建与 Unity 实际动态渲染像素。重建的数量参数来自 `Txt_Num`：字号 28、FontStyle 1、白色、黑描边 alpha 0.65882355／位移 `(1,1)`，再继承 0.7×2/3 缩放。

原生模板未知位数解码目前在 47 个亮态样本中正确 36、拒识 11、错误接受 0；25 个暗态全部拒识，7 个合成负例全部拒识。已知位数逐字分类为 47/47，仅为诊断。原生与 OCR 的本轮持久结果、模型与报告指纹见 `native_ocr_comparison.json`；此前 `calibration_summary.json` 仍代表旧截图模板那一轮。

供图校正和正式项目测试分开：本次仅授权在这四张图片上比较模板及人工标注；没有运行 pytest、打包、游戏输入或服务器回读。少量同源截图和复用像素不能作为实机准确率。阈值仍需后续实机调整。

重新制作时从仓库根目录调用以下工具，临时目录和生成报告保持在仓内 `.pytest_tmp/` 的专用子目录；控件、商品及旧截图数字使用 `prison_warehouse_calibration_20261023/`，原生数字及 OCR 使用 `prison_warehouse_native_ocr_20261023/`：

- `tools/build_prison_warehouse_transfer_templates.py`：默认读取这里的五张原图，制作页面结构、按钮锚点及内部状态裁片，用正式识别器输出回放报告。新增截图可用 `--stable-source` 和 `--stable-client-crop LEFT TOP RIGHT BOTTOM` 显式指定；默认报告写入 `.pytest_tmp/warehouse_state_separation_20261024/controls/`。
- `tools/build_prison_warehouse_goods_templates.py`：需提供本机 `--game-data`、开发工具源码及隔离的 UnityPy 环境；`--calibration-fixtures` 指向这里的 `goods_fixtures.json`。该环境只用于离线素材制作，不是自动化程序运行依赖。
- `tools/calibrate_prison_warehouse_quantity.py`：制作数量字形模板并比较人工数量标注，人工抄录保存为 `quantity_samples.json`；报告区分建库样本与其他样本、真实截图与合成诊断。`--font` 是可选的原生字体路径，仅用于生成未校正的 8／9；缺少字体时不会生成这些候选，不影响 0～7 的供图制作。
- `tools/build_prison_warehouse_native_digits.py`：从游戏 `font.asset` 提取绑定 TTF，并核对仓库 Text 与 Outline 参数。生成完整 0～9 的 RGBA、填充、描边、掩码与浮点响应；108 组渲染参数记录和未知位数报告保持在 `.pytest_tmp/prison_warehouse_native_ocr_20261023/native/`。可用 `--game-data`、`--unitypy-dir` 指定本机游戏和离线 UnityPy 目录。
- `tools/probe_prison_warehouse_ocr.py`：使用现有 `ppocrv6_small` CPU 模型比较 8 种固定预处理和 2 条管线，报告保存在 `.pytest_tmp/prison_warehouse_native_ocr_20261023/ocr/`。只调用图片识别 API，保留原始字符串与分数；不改生产配置、模型、字典或依赖。
- `tools/calibrate_prison_warehouse_ocr_roi.py`：只比较正常供图的全局 OCR 裁剪区域，`--verify-reader` 直接调用正式数量 reader 和真实现有 OCR 引擎，报告保存在 `.pytest_tmp/prison_warehouse_ocr_roi_20261023/`。人工破损完全排除，不参与本轮选型或验收。

OCR 识别与准入分开：固定 ROI 直接识别的原始文字在 47 个亮态数量样本中精确匹配 46 个，加入单结果、正整数、分数不低于 0.80 的规则后为 44 个正确、3 个拒识、0 个错误接受；25 个暗态样本为 24 个正确、1 个拒识。7 个白条／破损负例中仍有 3 个被高分错误接受，即使门限升至 0.99 也未排除。三处 20、两处 74/1003 和一处 94/1003 均读对。原生 0～9 重建字样的 OCR 另作合成诊断，不加入真实截图准确率。

货物身份和数量人工标签是校正基准，不能用同一识别器的输出反过来充当真值。页面切换、补点等待、逐项取货、复扫和返回已接入 `src/actions/_prison_warehouse_runtime.py`；这些素材工具只负责离线制作与供图分析，不执行取货流程。运行实现尚未实机验证。
