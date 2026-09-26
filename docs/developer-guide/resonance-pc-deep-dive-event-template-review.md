# 事件选项三态模板审阅

2026-09-26。当前为离线审阅样张，未接入任务，也未执行游戏点击。

## 本次产物

- 从本地 `RogueEventFactory`、`ActivityListFactory`、`RogueOrderFactory` 读取非 informal 的 Options 事件：71 个事件记录、192 个选项记录、180 组去重标题及说明。
- 每组生成 normal / disabled / selected 三态，共 540 张 PNG。三态全量展示用于比较，不代表每个选项都有可达的禁用状态。
- [可视化审阅页](images/resonance-pc-deep-dive-simple/event-template-review/index.html)。
- [真实截图和合成模板对照](images/resonance-pc-deep-dive-simple/event-template-review/actual_vs_generated.png)。
- [模板与效果目录](images/resonance-pc-deep-dive-simple/event-template-review/catalog.json)。目录禁止标记目前覆盖直接 Action / StepNum 命令、正数 SpinNum，不能作为尚未分析的间接事件跳转的完整禁止判断。

## 合成依据

使用游戏本地导出的 SourceHanSansCN-Medium 字体和 Btn_Normal、Btn_Unavailable、Btn_Selected_Green 图片。原字体不复制到产物中。字体与配置哈希存于 catalog.json。

Prefab 标题字号 31 且启用 BestFit，文本框高 30；说明字号 25。1280×720 截图校准后，Pillow 字号 20 / 16 更接近实机字形。不可选文字 alpha 为 0.50196。文本宽度参考 425 个设计单位，在 720p 约 283 像素。Pillow 换行、栅格化和静态背景合成不等同于完整 Unity 渲染，故必须以截图比较，不宣称逐像素重现。

模板仅包含标题与说明的紧裁区域和中间的少量状态背景。没有确认按钮，也没有限制原因提示。蓝色模板表示已选中；后续实现应再次点击选项主体，而非定位确认按钮。

## 已执行验证

以用户提供的两项普通、两项含禁用、三项含选中截图，共 7 个选项作为样本。每个候选区域对全量 540 张模板做比较；原始样本 7/7 正确第一名，亮度乘以 0.85 / 1.15 后 14/14 正确第一名。这些属于开发样本，评分权重已参考它们调整，不是独立留出集测试，更不是实机运行成功率。

普通 NCC 会丢失亮度／颜色信息，曾把蓝色选中态排在灰色态后。当前审阅评分将同一 RGB 模板的 NCC、绝对像素误差和平均色度差组合，不另识别 B1/B2，不使用 OCR。评分和阈值尚未完成生产校准。

对用户指出的 A2（回血25%）与 A5（什么也不做），实际截图交叉匹配错误项 NCC：

| 方向 | 较大范围 | 紧裁范围 |
|---|---:|---:|
| A2 模板找 A5 | 0.589 | 0.502 |
| A5 模板找 A2 | 0.543 | 0.523 |

紧裁减少了这对选项的错误相似度，但回血15%与25%只差数字，第一／第二名差距仍很小。不得只用绝对高分或第一名决定自动点击；需要进一步验证差异字区域和完整选项组合校验。

## 尚未验证

- 四项布局的真实截图与连续动画帧。
- 全部180组内容、长标题缩放、长说明换行／溢出与不同分辨率。
- 同一选项在多帧背景变化中的三态稳定性。
- 模板内容正确并不代表其事件后续流程已经实现。

生成器：`tools/build_deep_dive_event_templates.py`，参数 `--config` 指本地游戏 BinaryConfig，`--assets` 指本地导出的字体／三个按钮背景，`--output` 指仓库内输出目录。

验证器：`tools/review_deep_dive_event_templates.py`，参数 `--templates`、`--samples`（截图路径、ROI、预期选项 ID／状态的 JSON）、`--font`。必须在仓库根目录执行，临时验证输出使用 `.pytest_tmp/event_templates/`。本次原始输入归档和 samples.json 位于该临时目录。
