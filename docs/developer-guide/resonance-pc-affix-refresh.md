# 刷新词缀小任务

当前范围：自动刷新，命中即停，保留候选供用户手动替换。自动替换已禁用。此次调整未运行测试、离线回放或实机，不消耗胶卷；下方历史记录不表示当前版本已经验证通过。

## 范围和入口

从已经打开的装备词缀替换窗口开始，要求 1280×720 RGB 客户区。任务不导航仓库，不选择旧词缀，不滚动列表，不确认替换，不删除候选，不使用定向胶卷。

- `tasks:affix_refresh_pc.yaml:affix_refresh_read_pc`：只读窗口，返回候选、胶卷余额等精简快照及完整目标目录 `eligible_affixes`，不点击。
- `tasks:affix_refresh_pc.yaml:affix_refresh_pc`：接收 `config`，执行刷新及刷新消耗确认，命中后结束。
- GUI 入口为“小任务 → 刷新词缀”，沿现有 runner、CID 互斥、进度过滤和取消链运行。

GUI 只提供目标词缀、数值要求、胶卷上限和开始/停止按钮。目标可搜索完整词缀目录，不根据当前装备筛选；完整目录包含其他装备才能获得的效果，用户应选择适合当前装备的目标。页面提示先打开游戏的词缀替换窗口，命中后手动替换。运行时显示当前候选、已消耗/上限、余额和状态，并提供识别日志入口。目标、数值规则及预算可保存；不保存装备、旧槽位或运行快照。

`config` 使用 `target_affix_id`、`comparison`（`any/gte/eq/max`）、`threshold`（显示单位的十进制字符串）、`max_film_cost`（0～9999）。兼容字段 `auto_replace` 固定为 `false`；旧配置传入 `true` 也不会启用替换。无需 `snapshot` 或 `replace_index`，旧字段不构成执行前提。

## 业务流程

启动重新读取词缀窗口、胶卷余额和已有候选。装备名称、等级、剩余替换次数、旧词缀内容、数量、锁定情况及列表滚动位置均不参与识别或执行前提。

先判断已有候选，再判断是否消耗胶卷。候选满足目标时返回 `completed/target_found`、`matched: true`、`replaced: false`，不再输入。预算为 0 时只检查已有候选，不能刷新。

未命中时，每次刷新前重新观察并检查余额、预算，预留一次消耗，再发送一次刷新点击。只处理精确识别的刷新消耗确认框，不全局按回车。必须读到余额减少 1 且候选可识别，才将预留计入已确认消耗；同文案再次出现也可能是一次有效刷新，不能只按文案变化判断扣费。

最后一次预算内刷新结果仍先判断是否命中。预算用尽、资源不足为业务停止。结果不明确时停止，不重发可能扣费的点击；未确认消耗保留 `film_reserved: 1` 和不确定状态。余额未知以 `null` 表示：可以检查已有候选，但不能发起新刷新。

任务命中后不读取或定位旧词缀，不触发替换确认。用户需要自行处理候选；装备有多行、未解锁槽位或列表已经滚动不会触发本任务的旧列表识别。

## 识别、日志与数据

固定提示、刷新按钮、消耗确认弹窗使用模板；胶卷余额使用原生数字字形分割匹配，不回退 OCR。仅上方新候选的效果与数值区域使用 OCR，依据完整词缀目录及合法数值档位匹配，不猜测近似词缀。

候选小区域以完整像素摘要和词缀池为键缓存成功结果（LRU 上限 256 项）。图像变化重新识别，不能以旧文本兜底。每帧仍重新截图并核对窗口和余额；缓存不会跳过输入前检查。

每次实际 OCR 在 INFO 日志保留调用序号、用途、ROI、全部原文/置信度/位置和服务状态，随后记录解析的 ID/数值或失败原因。原始结果先于置信度与文案校验保存，服务异常也会记录；缓存命中不算新的 OCR。进度与结果通过 `ocr_log_path` 提供当前托管日志文件路径；GUI 按本次 CID 增量读取 OCR 记录，支持折叠查看和打开完整文件。

`tools/build_affix_refresh_catalog.py` 开发时从本机 `BinaryConfig` 导入目录、数值档位和确认文案，产出带来源指纹的 `data/meta/affix_refresh.json`。运行无需游戏安装资源或开发工具目录，也不调用游戏业务函数或发送游戏网络协议。

`tools/build_affix_refresh_templates.py` 从原生 prefab、字体产出模板与 manifest。`--reference-screenshot` 可对实际出现的标签和单个数字校准渲染差异，保留原生模板并记录截图 SHA256、裁剪及字形来源。现有资源包含旧版装备名称等模板，但纯刷新观察器不依赖这些识别。数字逐字分割，不保存整段余额模板。

## 保存截图的离线回放工具

`tools/replay_affix_refresh.py` 将指定图片裁为 1280×720 客户区，通过正式模板服务、OCR 服务和观察器识别，再观察同一帧以记录候选缓存表现。不加载游戏目标或操作运行时；截图仅返回文件像素，输入接口拒绝点击。输出及临时文件必须在仓库内。

工具支持可选预期：`--expect-film`、`--expect-no-candidate`，或 `--expect-candidate-id` 配合可选的 `--expect-candidate-value`。不再假设指定装备、等级、三条旧词缀或剩余替换次数。未提供预期时，`whole_frame_passed` 仅代表两次观察有效、字段一致且无输入；`expectations_supplied` 标明是否进行了外部预期比对，不能将无预期的结果视为识别正确性的独立证明。

仅在用户授权测试后，从仓库根目录执行，例如原起始截图：

```powershell
& ./.venv/Scripts/python.exe -B tools/replay_affix_refresh.py --image '起始截图绝对路径' --expect-film 885 --expect-no-candidate
```

本次只修改工具，未执行上述命令。

## 历史验证记录（旧版实现，2026-10-21）

旧版起始截图报告 `.pytest_tmp/affix_refresh_start/report.json` 曾识别不可见之触 LV59、胶卷 885、替换次数 995、无候选及三条旧词缀。首次旧词缀 OCR 3 次，重复观察 0 次、命中缓存 3 次；实机截图及输入均为 0。报告使用同一张截图校准和回放，不能推广为其他装备或状态验证。此报告属于旧版观察器，不能当作当前纯刷新版本的运行结果。

旧版首次实机通过正式 runner/task 运行，目标为靡草龙雀 LV67 的最终伤害至少 4%，覆盖第一条旧词缀，胶卷上限 10。只读正确识别余额 884、替换次数 995、候选虚光伤害 4% 及三条旧词缀。

CID `768054132815695872` 发送一次刷新点击，消耗确认框未匹配，30 秒后 `failed/observation_timeout`，未重试。原始业务结果为 `film_spent=0`、`film_reserved=1`、`result_uncertain=true`。退出后单独取消未确认弹窗，核对余额仍为 884、实际消耗 0；该后续核对不抹去原任务的不确定性记录。

`.pytest_tmp/affix_live/` 保存旧版配置、派发、进度、结果、截图和日志，`ocr.log` 汇集各阶段 OCR 原文及解析。没有第二次实机刷新。弹窗模板随后用保存现场图离线修正，曾识别为 `refresh_confirm`，未验证成功扣费、预算耗尽或命中完整流程。当前纯刷新版本亦未验证这些流程。

## 进度与结果

进度事件 `task.resonance_pc_affix_refresh_progress`，schema 为 `resonance_pc.affix_refresh_progress.v1`，携带 CID 和单调 sequence。结果位于 `affix_refresh`，包含 mode/status/reason/message、film_spent/film_reserved/film_limit/film_remaining、current_candidate、matched/replaced/result_uncertain；`auto_replace` 恒为 `false`。

`completed/target_found` 和 `matched: true` 表示找到目标并保留候选，`replaced` 恒为 `false`。`stopped` 表示预算或资源使任务结束。GUI 区分命中、预算用尽、资源不足、取消和异常，展示具体原因及待确认消耗，不报告自动替换成功。

框架取消及失败覆盖业务完成状态，面板保留最后阶段和消耗信息。取消后不再发起新输入，已启动的工作线程退出后才释放运行锁，不关闭游戏窗口、不删除候选来清理现场。
