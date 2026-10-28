# 领取成品并取货（PC）

公开任务：`tasks:collect_and_withdraw_products_pc.yaml:collect_and_withdraw_products_pc`。
领域为整顿中心，独占运行。客户区必须为 1280×720，起点为已经停靠的城市建筑／NPC 界面（`city_panel`）；正常结束必须确认回到同一城市的建筑界面，城市键一致。带出发按钮的列车／游戏主界面（`city_main`）不是此任务的起止页面。本任务不规划城市移动。

## 输入

| 参数 | 规则 |
| --- | --- |
| `operations` | 多选列表，默认 `[collect_products, withdraw_products]`；固定先领取、后取货，不按输入顺序重排。重复项去重，未知操作拒绝。 |
| `goods` | 可选商品名称或 ID 字符串列表。不传、`null` 或 `[]` 表示尝试当前可见的全部达标成品，不要求屏外全取；指定清单时解析为 ID、去重并检查图标支持状态。非法名称或空字符串不解释成全拿。 |
| `quantity_threshold` | 启用取货时必填、非负整数，保留原有库存筛选含义，不是取货配额。 |

未启用取货时，不要求后两个参数。`operations=[]` 会只读确认城市建筑界面，返回 `skipped`，不进入整顿中心。非法取货参数由首个节点拒绝，不启动任何游戏输入。

```json
{
  "operations": ["collect_products", "withdraw_products"],
  "goods": ["涤纶"],
  "quantity_threshold": 1
}
```

## 执行与交接

YAML 声明顺序和条件，使用标准 `aura.run_task` 调用领取、取货两个完整子任务：

1. 只读校验参数，使用现有城市徽章模板连续两帧确认城市建筑界面，并记录 `city_key`。
2. 从该城市直接进入整顿中心：复用城市商店数据中的 `reforming_center` 坐标，输入前从同一新截图再次核对城市徽章，单击一次后等待普通总览连续两帧确认。没有该入口的城市在输入前拒绝，不通过头像菜单或列车主界面绕行。
3. 按开关调用 `collect_product_workshops_pc`；检查业务结果及返回总览证据。
4. 按开关调用 `withdraw_product_warehouse_to_train_pc`；传入已规范化的商品 ID 与阈值，保留本次实际装车数量。取货子任务不回顶、不上下拖动或滚轮，只按当前顺序处理可见完整格子，每个商品 ID 最多点击一次；点击后新截图重新定位，可继续处理删栈后自然进入视口的新格子。
5. 新截图确认普通总览及共享返回箭头后，点击返回并连续两帧核对原城市徽章；出现目标或过渡状态后不追点。仅仍明确停留在原总览时允许有限补点，已在同城建筑界面时零点击通过。不使用返回列车主界面的按钮。
6. 汇总启用项、已完成结果和最终页面。两个子任务原有独立入口保持可用。

门控动作仅解释一个阶段的结果，不调度任务。节点完成不替代业务完成；跳过节点明确保留 `skipped` 或 `not_processed`，不能补成已执行。没有业务重试循环，也不会因后续失败重跑已经领取或搬运的步骤。

取货没有可见达标候选时以 `stop_reason=no_visible_eligible_goods` 停止，车满以 `train_full` 正常停止。之后只做一次当前视口只读稳定复查，不滚动复扫，也不恢复取货。城市建筑界面 `city_panel` 起止、领取分支、参数及正常返回规则均保持不变。

## 阻断与失败

- 领取正常完成，包括所有开放车间都没有产物，可以继续取货。
- 只有领取返回 `status=blocked, success=false, reason=warehouse_capacity_blocked`，且已取消丢物提示、确认回到总览时，允许继续启用的取货；领取结果保留。取货腾出空间后，本轮不再次领取。
- 其他领取失败停止取货；仅有明确返回总览证据时才允许安全回城。
- 取货车满沿用子任务正常结束；指定商品时子任务 `partial` 不提升为整体完成。不指定商品的已知余货例外见下一项。
- 不指定商品时只要求完成一轮当前可见范围的取货尝试，不要求仓库清空或屏外全取：车满按正常结束；当前可见范围确认、数量核算和返回总览均明确，且子任务 `partial` 的唯一问题为已知达标余货时，也允许组合正常完成。子任务原始 `partial` 和 `remaining_targets` 仍保留，不伪造已取尽。
- 页面未知、搬运结果不明或子任务框架失败而没有可信结束页面，不盲目返回或重放。框架失败没有输出时，已执行取货的 `goods_taken` 和 `total_taken` 为 `null`，`withdrawal_quantity_known=false`，不冒充零搬运。
- 取消透传原生 `CancelledError`，不执行后续取货或收尾点击。
- 回城失败不抹掉之前已完成的领取、装车及原始失败原因；最终页面未确认时返回 `unknown`。

## 结果

主要字段：`success`、`status`、`reason`、`operations`、`city_key`、`collection_result`、`withdrawal_result`、`goods_taken`、`total_taken`、`withdrawal_quantity_known`、`returned_to_city`、`page_after`、`failure_stage`、`issues`。成功时 `page_after=city_panel`。

`goods_taken` 只描述本次实际新增装车，不把领取车间数当作商品数量。未启用的子任务标记 `skipped/disabled`，因前置失败未执行的启用项标记 `not_processed`。取货未执行时新增量为已知的 0；已执行而无法核算时明确未知。

`withdrawal_result.scan_scope="visible"`；`scan_complete` 仅表示当前可见范围已确认，不表示全仓覆盖。`inventory_complete` 仅在成品区明确为 EMPTY 且没有未知项时为 `true`，否则为 `false`。屏外未见商品不能当作库存 0；实际装车量按已确认的同商品取货前后数量核算，已点击商品的数量仍未知时失败，不盲目回城。

整体 `success=true` 用于启用项达到各自目标、且最终城市页面明确；不指定商品的取货目标是一轮可确认的可见范围尝试，不是全清空。空操作列表正常返回 `success=true, status=skipped`。有已完成操作后遇到阻断、识别／可见扫描不明的子任务 `partial` 或回城失败时返回 `success=false, status=partial`；无已完成操作时保留 `blocked` 或 `failed`。

最终动作把完整汇总写入根执行上下文的 `framework_data.product_workflow_result`。整体失败同时抛出 `StopTaskException(success=False)`，不会把业务失败包装成框架成功。当前框架失败任务不渲染普通 `user_data/returns`，应从该诊断字段查看组合结果；成功或跳过时普通返回为同一汇总对象。取消仍保持框架取消，不强行生成成功或执行回城。

## 验证边界

2026-10-24 无滚动改动已通过 162 项离线回归，包含无筛选可见范围取货及门控、无滚动编排、数量核算、仓库状态识别、真实 DAG／任务清单与可见成品快照兼容性。清单同步、check、validate 通过；plan doctor 为 0 error，176 项既有缓存警告。报告位于 `.pytest_tmp/warehouse_no_scroll_20261024/`。本次未运行实机。

以下测试和实机记录均发生在本次移除仓库滚动之前，是旧版历史证据，不作为当前无滚动流程的验收。

此前单独取货任务的涤纶实机结果不能替代组合任务验收。组合首次实机（CID `769195577354227712`）暴露了起始页面误把城市建筑界面当作带出发按钮的游戏主界面的问题，在预检停止，没有领取或取货。已据用户澄清将起止边界统一为 `city_panel`，城市建筑直达整顿中心、返回箭头回到同城；后续实机记录另列。

2026-10-24 城市边界修正后的验证：68 项组合单元测试及 53 项真实 DAG、任务清单和仓库识别回归测试通过，共 121 项。导航测试包含真实城市截图与实际徽章读取器回放、同城校验、实际 Frame／InputSession 输入保护、直接建筑入口以及返回箭头的真实 transition 机制。清单同步、check、validate 通过。报告与日志位于 `.pytest_tmp/product_workflow_20261024/`。此前单独取货和组合参数／门控测试的通过记录不能替代此次城市界面回归。

### 只取货的城市闭环实机（旧版含滚动流程）

- 最终任务 CID：`769200087640313856`；`operations=[withdraw_products]`、`goods=[涤纶]`、`quantity_threshold=1`。
- 起始城市为云岫桥基地建筑界面，直接单击该城市整顿中心入口 `(940,190)`，没有通过列车主界面／头像菜单进入。
- `collect` 节点实际 `skipped`，结果 `enabled=false, status=skipped, reason=disabled`，没有领取成品。
- 取货子任务完成：仓内涤纶 20 → 0、成品区 EMPTY，列车载货 56/1003 → 76/1003，本次新增涤纶 20；扫描和核算完整，无 issues。
- 总览返回箭头点击一次后确认云岫桥基地徽章连续两帧，最终 `success=true, status=completed, returned_to_city=true, page_after=city_panel, city_key=yunxiuqiao_base`。
- 证据：`.pytest_tmp/product_workflow_withdraw_city_panel_final_20261024/` 的 `before.png`、`after.png`、`result.json`、`combination_result.json`、`warehouse_result.json` 和逐帧截图。运行器已关闭；没有额外重进仓库做服务器回读。
- 中间一次入口实现验证（CID `769199797067321344`）因漏传操作内 `InputSession` 被框架拒绝，没有发送城市入口点击；修复并用实际输入保护回归后才完成上述最终实机。

本次只验收只取货、单商品、单视口、容量足够的城市闭环。领取成品分支、两项同时启用、满车或多视口等分支仍没有本次实机证据。

后续按用户补充，组合入口已支持不指定商品时尝试全部达标成品；省略、null、空列表统一传给原取货子任务的无筛选模式。96 项组合单元／真实 DAG 契约测试通过，覆盖多商品核算、缺省参数转发、仅已知余货可接受及未知证据仍拒绝。该补充只离线验证，没有再次运行游戏。
