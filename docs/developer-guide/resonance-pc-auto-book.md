# PC Auto Book

Auto Book 将手动进货书预算改为单书收益门槛：每条候选城市边和议价组合，只有新增一本书
带来的税后预计利润严格大于 `book_profit_threshold` 才接受该书数，然后参与全局路线搜索。
例如门槛 500000，连续三本的增量分别为 800000、600000、500000，自动模式只接受前两本。
手动模式继续使用有限 `book_budget`，恰好等于门槛时仍允许使用。

## 使用入口

- 工作流程中的快捷货运、完整货运参数：模式同步，沿用 `trade/inputs_json` 保存。
- 小任务中的跑商试算：独立使用 `trade_preview/inputs_json` 保存；首次创建时从正式货运复制。
- 默认关闭。开启后手动书数输入框禁用但保留值；关闭后恢复编辑。
- 默认门槛为 500000，不覆盖用户明确保存的旧门槛。
- 自动模式不受背包真实书数约束；不新增背包扫描或缺书处理。

## 参数链

两条任务 `tasks:auto_cycle_trade_pc.yaml:auto_cycle_trade_pc` 和
`tasks:preview_trade_plan_pc.yaml:preview_trade_plan_pc` 均接受 `auto_book: boolean`。
组合任务在 `trade_inputs` 下声明同一字段，两种客货运顺序都传给货运。

GUI 保存完整手动配置，在提交边界通过 `normalize_trade_task_inputs` 剔除自动模式下的
`book_budget`。YAML 可以补出默认 0，外部调用方也可传非零值；规划服务会统一忽略该预算。
缓存键区分模式，自动模式中的手动书数不产生不同缓存，门槛等有效参数变化会产生不同缓存。

恢复复用现有精确求解器及其二分选书、无书本预算维度的稀疏搜索，不修改算法。
指定终点、可用城市、议价参数、气泡水选点及前置刷新保持原来的职责。
气泡水在 Auto Book 得到的最终路线上选择休息区，不因自动用书增加疲劳预算。

## 返回与展示

正式运行和试算均返回 `auto_book`、`book_budget_ignored`、`book_profit_threshold`，以及：

- `book_incremental_profit` 和精确值：每条最终路线边相对同城市边、同议价组合零书方案的增量之和。
- `average_book_profit` 和精确值：上述增量除以 `books_used`，不用书时为 `null`。
- 自动模式的 `books_budget`、`remaining_books` 为 `null`；`books_used` 为计划使用总书数。

正式运行方案和试算结果显示“平均每本进货书收益”，仅用书且字段有效时显示；旧历史缺字段也可查看。
规划进度事件和最终任务结果使用同一收益口径，不将总路线利润除以用书数。

## 验证

复用原求解器暴力对拍、严格阈值、路线变化及平局测试，恢复 GUI/action/task/service 契约测试。
新增 v1.9.0 独立试算设置、正式货运双向同步、组合嵌套输入、平均收益和气泡水共同开启测试。
冻结程序仍需通过 CPU 发布契约、运行时/GUI 自检；实际用书和交易闭环由实机验收确认。
