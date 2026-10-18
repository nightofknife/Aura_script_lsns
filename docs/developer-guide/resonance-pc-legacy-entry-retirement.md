# PC 旧入口退休

本次保留当前生产配员、黑月采购和精确货运流程，删除并行存在的旧公开入口及专属实现。旧名称不注册、不转发，也不提供旧模块兼容层。深潜及其共用依赖、框架和历史截图不在修改范围。

## 生产配员

当前入口为 `tasks:fill_product_workshop_staff_pc.yaml:fill_product_workshop_staff_pc`，继续使用 `product_workshop_staff_assets`、`product_workshop_staff_step` 和 `product_workshop_staff_summarize`。房间导航、原料车间及公共人员选择控件保留。

已退休 `resonance_pc.product_workshop_quick_fill`，删除其动作模块、`_product_workshop_ui.py`、`build_product_workshop_templates.py`、旧目录索引及 `workshop/production/controls/editor/` 下 15 张独占文字生成图。当前人员编辑器及 `build_product_workshop_staff_templates.py` 保留。这里的 15 张图属于旧公开实现的配套资源，区别于此前已删除的人员数字和标签废图。

## 黑月采购

当前任务仍使用 begin → prepare → select → submit → complete → finish 分阶段流程。第一批整理的准备/退出公共补点骨架、校准商品识别、单次提交、结算等待及当前账本保留。

已退休 `resonance_pc.purchase_black_moon_local_from_rest_menu`，删除旧单体采购模块、旧选品列表归一化、旧访问初始化及旧整库盘点会话专属代码。共享视觉模块保留准备、选品实际使用的图像处理和匹配方法；不能按旧模块名称整包删除。当前账本仍需要的读写锁、pending 状态、历史记录校验和防重复提交逻辑保留。

原生资源库现在只提供六个准备控件和 140 张 normal 商品源图，供当前校准生成器使用。旧 scanner 的 140 张 selected 商品变体、16 张控件图/mask、14 张卡框与锚点、1 张 120×119 商品识别 mask 退休，共 171 张。原生生成器同步停止输出这些资源；完整及 prepare-only 刷新均不重建旧资源。

原生库仍采用计划相对路径，校准库仍采用库相对路径。保留资源的字节、商品 ID 与来源信息保持；当前 96×96 校准库及历史来源摘要不重写。`sources/` 中的生成来源记录是证据。

## 规划器

当前规划入口为 `resonance_pc.trade_plan_optimal_route`，继续使用 exact 模型及 dense/sparse 两个执行后端。整单结算、税、议价、声望、商品解锁、采购书、当前缓存和城市约束保留。

退休动作：

- `trade_plan_next`
- `trade_plan_best_cycle`
- `trade_plan_cycle_execution`
- `trade_plan_next_cycle_execution`
- `trade_simulate`
- `trade_loop_init`、`trade_loop_update`、`trade_loop_summary`、`trade_loop_cleanup`

对应的旧滚动、beam、循环、持货模拟方法及专属常量、状态和缓存一并删除。`beam_width` 构造参数已移除。

当前 `trade_route_execution_init/update/summary/cleanup` 保留，继续记录部分完成路线、blocked 信息和确认消耗，并在结束时清理本次运行状态。内部 `_loop_state_key` 仍被当前路线初始化使用，其名称和状态键格式保持。

## 验证

退休检查覆盖旧模块、公开导出、工具及独占资源不存在，当前任务入口仍注册，并通过实际发布收集器验证当前目录及模板可打包。当前配员、黑月分阶段采购、账本、精确规划和路线生命周期使用原有回归及新增退休边界测试。

manifest 由 `package_cli sync` 生成。所有检查从仓库根目录执行，pytest 使用 `.pytest_tmp` 下独立子目录；其他工具的 TEMP/TMP/TMPDIR 也限制在仓库内。离线检查不代表新增实机验收。
