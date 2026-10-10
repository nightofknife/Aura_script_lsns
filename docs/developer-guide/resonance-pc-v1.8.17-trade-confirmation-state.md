# Resonance PC v1.8.17：交易结果确认与页面状态假成功

> 状态：问题分析占位，等待后续实现。
>
> 本文档记录用户日志中的第二轮跑商、v1.8.17 源码中已确认的业务成功判定缺口，以及当前证据无法确定的部分。本 PR 不包含业务代码修改，也不表示问题已经修复。

## 问题摘要

用户反馈跑商过程中货物没有成功卖出。第二轮日志最终明确报错在“完成阿妮塔战备工厂交易后，找不到返回城市主页按钮”，而不是直接报错于卖货函数。

不过 v1.8.17 源码中存在两个能够解释“货物没卖出但程序没有立即报错”的结构性问题：

1. 卖货未确认时仍返回 `success=True` 和 `page_state="shop_page"`，上层也不检查 `sold_confirmed`。
2. 买卖共用的结算关闭函数在第二次点击退出后不再确认页面是否消失，却无条件返回 `closed=True`。

因此，动作调用成功、按钮点击成功、业务交易成功和页面恢复成功在当前实现中没有被严格区分。

## 复现记录

- 版本：`v1.8.17`
- 标签提交：`2ef9f773b87e4f21140d67582fbf8c5b7402b73d`
- 任务：`Exact Auto Trade (PC)`
- CID：`750744185560109056`
- 客户端分辨率：`1280x720`，分辨率检查通过
- 执行时间：`2026-09-03 23:49:22` 至 `2026-09-04 00:16:52`

## 第二轮日志时间线

日志没有显式写出起点城市名称，但后续路线和交易节点可以还原如下：

| 时间 | 阶段 | 日志结果 |
| --- | --- | --- |
| 23:49:22.017 | 开始第二轮 `Exact Auto Trade (PC)` | 正式任务开始 |
| 23:49:36.648～23:49:36.953 | 起点交易所 | 菜单连续两次稳定命中 |
| 23:51:58.524 | 选择阿妮塔战备工厂 | 命中并点击 |
| 23:58:40.315 | 到达阿妮塔战备工厂 | `city_main_detected`，耗时 390.203 秒 |
| 23:58:55.371～23:58:55.676 | 阿妮塔交易所 | 菜单稳定 |
| 23:59:09.333～23:59:13.093 | 阿妮塔买货 | 使用 7 本进货采买书，返回买货页模板稳定 |
| 23:59:28.547～23:59:32.170 | 阿妮塔买货 | 砍价两次，达到配置尝试上限 |
| 00:00:44.437 | 选择淘金乐园 | 命中并点击 |
| 00:05:29.051 | 到达淘金乐园 | `city_main_detected`，耗时 275.657 秒 |
| 00:05:41.860～00:05:42.165 | 淘金乐园交易所 | 菜单稳定 |
| 00:05:47.950～00:05:54.232 | 淘金乐园卖货 | 执行两次抬价，达到配置尝试上限 |
| 00:08:12.467 | 选择修格里城 | 命中并点击 |
| 00:10:33.217 | 到达修格里城 | `city_main_detected`，耗时 131.812 秒 |
| 00:10:46.302～00:10:46.607 | 修格里城交易所 | 菜单稳定 |
| 00:10:53.743～00:10:56.437 | 修格里城买货 | 使用 2 本进货采买书，返回买货页模板稳定 |
| 00:13:01.970 | 再次选择阿妮塔战备工厂 | 命中并点击 |
| 00:15:42.472 | 再次到达阿妮塔战备工厂 | `city_main_detected`，耗时 148.484 秒 |
| 00:15:56.633～00:15:56.938 | 阿妮塔交易所 | 菜单稳定 |
| 00:16:04.154～00:16:08.091 | 阿妮塔买货 | 使用 7 本进货采买书，返回买货页模板稳定 |
| 00:16:25.702～00:16:29.116 | 阿妮塔买货 | 砍价两次，达到配置尝试上限 |
| 00:16:52.377 | 返回城市主页 | 必需导航按钮模板未找到，任务失败 |

最终异常：

```text
required navigation button template was not found; click skipped
```

日志调用栈：

```text
resonance_pc_auto_cycle_trade_flow
  -> _execute_route
  -> _execute_trade_leg
  -> _execute_city_trade_inside_current_city
  -> _execute_city_trade_inside_current_city_scoped
  -> resonance_pc_go_city_main_direct
  -> _click_required_nav_button
```

对应 v1.8.17 源码行：

- `city_trade_flow_pc_actions.py:2009`
- `city_trade_flow_pc_actions.py:1106`
- `city_trade_flow_pc_actions.py:1228`
- `city_trade_flow_pc_actions.py:986`
- `city_trade_flow_pc_actions.py:1052`
- `city_trade_flow_pc_actions.py:607`
- `city_trade_flow_pc_actions.py:516`

城市主页按钮配置为：

```text
template=templates/nav_city_main_button.png
region=[140, 0, 130, 80]
threshold=0.86
timeout=3.0 seconds
```

## 已确认缺陷一：卖货未确认仍返回成功

`resonance_pc_sell_goods_on_sell_page` 依次尝试：

```text
寻找并点击“全部卖出”
  -> 可选抬价
  -> 寻找并点击“卖出”
  -> 等待卖出结算模板
  -> 根据 settlement.closed 计算 sold_confirmed
```

当结算未确认时：

```python
sold = bool(settlement.get("closed"))
```

但返回结果始终包含：

```python
{
    "success": True,
    "page_state": "shop_page",
    "sold_confirmed": sold,
    "sell_result": "sold" if sold else "empty_or_no_result",
}
```

相关位置：`plans/resonance_pc/src/actions/city_trade_flow_pc_actions.py:880-967`。

这会把以下业务状态混为一类：

- 当前确实没有货可卖；
- 实际有货，但“全部卖出”没有识别或点击成功；
- “全部卖出”成功，但“卖出”按钮没有识别或点击成功；
- 卖出动作发生，但结算模板没有识别到；
- 结算出现，但页面没有可靠退出。

除“请求抬价但全部卖出未选中”这一分支外，其余未确认场景可以继续向上返回 `success=True`。

## 已确认缺陷二：上层不检查业务卖出结果

`_execute_city_trade_inside_current_city_scoped` 的顺序为：

```text
进入交易所
  -> 等待交易所菜单稳定
  -> 进入卖货页
  -> 调用卖货动作
  -> 不检查 sell.sold_confirmed
  -> 如有采购计划，直接进入买货页
  -> 调用返回城市主页
```

相关位置：`plans/resonance_pc/src/actions/city_trade_flow_pc_actions.py:1002-1066`。

因此存在以下实际可达的错误链：

```text
实际有货
  -> 卖货识别或点击失败
  -> sold_confirmed=False
  -> 卖货动作仍 success=True
  -> 上层忽略 sold_confirmed
  -> 继续买货和后续路线
  -> 用户看到旧货仍在车里，但卖货步骤没有立即报错
```

这解释了用户反馈为何可能与最终的导航异常同时存在，而不要求两者发生在同一个步骤。

## 已确认缺陷三：结算关闭存在假阳性

`_close_settlement` 当前行为：

```text
等待买入/卖出结算模板
  -> 未发现：closed=False
  -> 已发现：点击固定退出点 (640, 620)
  -> 等待并重新检查
  -> 若模板仍存在，再点击一次
  -> 不再检查第二次点击后的页面
  -> 无条件返回 closed=True
```

相关位置：`plans/resonance_pc/src/actions/city_trade_flow_pc_actions.py:447-492`。

因此 `closed=True` 在重试分支中只证明：

> 曾识别到结算模板，并尝试点击退出一次或两次。

它不能证明：

> 结算模板已经消失，且交易所商店页已经重新出现。

买货和卖货共同使用该函数，因此错误页面状态可能同时污染两个流程。

## 买货侧的同类问题

`resonance_pc_buy_goods_on_buy_page` 会根据 `_close_settlement("buy")` 计算内部变量 `bought`，但最终同样始终返回：

```python
{
    "success": True,
    "page_state": "shop_page",
    ...
}
```

返回结构中甚至没有与 `sold_confirmed` 对称的顶层 `bought_confirmed`。上层同样不验证买入业务结果。

相关位置：`plans/resonance_pc/src/actions/city_trade_flow_pc_actions.py:708-870`。

第二轮最后一次阿妮塔交易已经明确进入买货流程、使用采买书并执行砍价，随后才在 `go_city_main_direct` 失败。因此较符合执行时序的一种解释是：买入结算或确认页面没有真正退出，但买货动作仍声明 `page_state="shop_page"`，上层随后在错误页面寻找城市主页按钮。

这只是高可信推断，不是日志直接证明；失败瞬间没有截图，异常字符串也没有序列化 `_click_required_nav_button` 保存的最后匹配详情。

## 可观测性缺口

卖货和买货动作通过进度事件上报业务字段，但当前会话日志没有落下这些事件的完整 payload。日志中缺少：

```text
sell_all_click
sell_button_click
sold_confirmed
sell_result
buy_button
bought / bought_confirmed
settlement.first_match
settlement.recheck
结算第二次退出后的页面检查
```

因此，这份日志不能单独确定第二轮究竟在哪一座城市第一次漏卖，也不能确定 `00:16:52` 的实际画面是买入结算页、确认页还是正常商店页上的模板漏检。

## 证据结论

### 日志直接证明

- 第二轮完成了多段运输，并在最后一次阿妮塔战备工厂交易后失败。
- 最终异常发生在 `resonance_pc_go_city_main_direct` 查找城市主页按钮时。
- 异常不是由卖货函数直接抛出。

### 源码直接证明

- `sold_confirmed=False` 时卖货动作仍返回 `success=True`。
- 上层不检查 `sold_confirmed`，会继续进入买货。
- 买货动作也会无条件声明 `success=True` 和 `page_state="shop_page"`。
- `_close_settlement` 第二次点击后没有验证结算页是否真正消失。

### 尚不能证明

- 第二轮第一次漏卖发生在哪座城市。
- 最后一次导航失败时屏幕具体停在哪个页面。
- 城市主页按钮失败究竟是页面状态错误还是模板本身漏检。

## 后续修复需要覆盖的边界

1. 为卖货定义互斥、可验证的业务结果，例如 `sold`、`no_cargo`、`failed`、`unknown`；不得继续使用 `empty_or_no_result` 混合正常空仓与操作失败。
2. 为买货提供对称的 `bought_confirmed` 或结构化业务状态。
3. `success=True` 只表示满足该动作承诺的业务后置条件，不能仅表示代码没有抛异常。
4. 结算关闭必须同时验证“结算页消失”和“预期商店页重新出现”，第二次退出后也必须复查。
5. 上层在进入买货、返回城市主页或开始下一段运输前，必须检查上一业务步骤的结果和实际页面状态。
6. 对未知状态实施有限重试、恢复或明确失败，不允许携带伪造的 `page_state` 继续点击。
7. 将每站的卖货、买货和页面恢复结果写入会话日志，确保现场问题能够定位到具体城市和步骤。

## 建议验收场景

- 空仓进入卖货页：明确返回 `no_cargo`，允许按策略继续。
- 有货且正常卖出：只有结算完成并回到商店页后才返回 `sold`。
- “全部卖出”未找到：有货/未知时不得伪装为空仓成功。
- “卖出”按钮未找到：不得继续进入买货。
- 卖出结算页第一次退出失败、第二次成功：复查后才确认页面恢复。
- 两次退出都失败：返回明确失败，不得声明 `page_state="shop_page"`。
- 买入结算关闭失败：不得调用城市主页导航。
- 城市主页按钮模板漏检：日志包含最后匹配置信度、ROI 和失败瞬间截图或受控诊断图。

## 与另一问题的关系

同一份日志的第一轮在抵达海角城后被自动蜃息岛投资指标读取异常中断，因此尚未进入海角城卖货。该问题独立记录在另一个占位 PR。本 PR 只处理交易业务确认、结算关闭和页面状态契约，避免两个根因相互遮蔽。
