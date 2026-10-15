# 生产车间补人（PC）

任务：`tasks:fill_product_workshop_staff_pc.yaml:fill_product_workshop_staff_pc`，展示名为“生产车间补人”。起点为普通整顿中心总览，客户区固定为 1280×720；首版入口支持现有导航素材覆盖的 1 号、2 号生产车间。调用时必须传入 `room_name`。

## 已确认流程

1. 复用整顿中心导航打开指定生产车间。
2. 点击车间编辑。
3. 点击快速编队。
4. 点击一键选中；已满时跳过这个按钮，绝不点击一键取消。
5. 点击确认，并核对编辑页人数与已选人数一致。
6. 点击左上角返回，等待是否保存提示。
7. 在提示中确认保存应用。
8. 再次点击左上角返回，识别到普通整顿中心总览后结束。

“退出”指第 6 步的左上角返回，不是另加一个关闭人员选择面板的操作。选择确认只修改本地编队；不能把这一步当成已保存。保存提示中的原生按钮文案为“确认 / 取消”，代码用 `apply` 表示确认保存的业务含义。若已经满员且没有产生改动，原生返回可以直接离开而不出现保存提示。

任务使用游戏自带快速编队候选规则，保留现有人选并补空位，不另实现逐个囚犯选择或排序。客户端候选逻辑包含 Free/InPrepare；不要将这个任务描述成“仅选择完全空闲人员”或保证不会移动其他位置的待岗人员。

## 实现与失败行为

任务 YAML 管理顺序与失败后的跳过；`product_workshop_staff_step` 每次只执行一个步骤。每步重新截图识别前置页面、按钮和后续状态，确认、保存与返回均不盲目重试。匹配到提示即接受，不要求提示保持多帧，也不依赖保存成功提示一定出现。

全程使用页面、按钮及单字符数字模板；数字匹配是固定区域内与 0–9、斜杠字形图像比较，不调用 OCR 服务、模型或游戏运行时接口。编辑页整数与选择页分数按各自原生字体处理。

结果区分：

- `completed`：原本已满，无需增加人员，且最终总览已确认。
- `submitted`：本地编队已选满，保存应用已提交，且返回总览。`success=true` 表示这次 UI 提交闭环完成，不表示服务器保存结果已复核。
- `partial`：原生一键选中增加了人数但仍有缺口，已提交保存且返回总览。
- `failed`：未知页面、人数无法识别或不一致、按钮不符合预期、点击效果未确认、没有观察到新增候选，或返回总览未确认。后续写入步骤跳过，保留页面与点击证据。

当前没有候选且一键选择人数未增加时，会停止在选择页并报告失败；不会重复点击、清空名单或提交空名单。此行为保留现场，后续可以结合实机状态再补充无候选时的退出分支。

结果输出包括 `initial_count`、`selected_count`、`capacity`、`selected_added_count`、`remaining_count`、`applied`、`returned_to_overview`、`page_after` 与逐步骤证据。人数缺口明确标为 `count_scope=local_formation`。原生保存回调发送异步请求后立即返回，既定流程没有额外重开编辑页复核；因此保存变化时 `save_verified=false`，`final_count` 与 `added_count` 返回空值，不将保存前本地人数当成服务器最终人数。框架节点成功不代表业务成功。

## 模板来源与验证边界

按用户要求，新增模板来自游戏原生素材、字体和预制体布局合成，不通过操作游戏采集。素材脚本为 `tools/build_product_workshop_staff_templates.py`，版本化目录为 `templates/product_workshop_staff/`，来源、布局和示例场景记录在 `data/meta/product_workshop_staff_templates_pc.json`。生产车间入口继续使用已有真实截图模板。运行时只读取随计划打包的模板，不要求本地安装目录存在或读取游戏数据。

主要原生逻辑依据：`UIPrisonEdit` 的 `ShowPrisonerList`、`OneBtnSelect`、`RefreshPrisonerListBottomPanel`、`RefreshTopInfo`、返回保存回调及 `SaveChanges`；`CommonTips.OnPrompt` 与 `UIPromptLuaBehaviour`。复杂反编译条件不能直接视为可靠布尔逻辑；实际操作顺序以用户确认流程为准。

`tests/fixtures/product_workshop_staff/` 是明确标注的合成场景。离线图像匹配、模拟输入和真实 DAG 测试只能验证实现与这些素材之间的一致性，不代表模板或保存返回流程已通过实机验收。本轮不运行游戏。
