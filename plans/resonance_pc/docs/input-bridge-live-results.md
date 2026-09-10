# 输入桥接最小原型实测

## EndDrag 后 StopMovement 实测

以相同仓库控件和 310 像素输入位移执行 0.5 秒拖动，holdMs=0；OnEndDrag 后同一主线程立即调用 ScrollRect.StopMovement。释放时 content.anchoredPosition.y=465.108642578125；释放后约 253、507、750、1003、1255、1503、1755、2000ms 的采样值全部相同。此次正常范围拖动没有释放后惯性漂移。对照此前不调用 StopMovement 的实验，释放后两秒从 465.11 增至 913.80。

框架 WGC 截图 stop_before.png / stop_after.png 位于 .pytest_tmp/bridge_live。回调与 GC handle 正常清理，脚本已卸载。未测试越界弹性回弹，不能将停止惯性扩展成任何情况下位置绝不变化的保证。

## 直接拖动惯性复测

再次在仓库执行 BeginDrag/Drag/EndDrag，移动 0.5 秒后立即释放（holdMs=0），不发送点击。EndDrag 后不再调用 Drag，只读 content.anchoredPosition 两秒。记录纵向位置：释放时 465.11，255ms 后 648.05，502ms 后 754.50，1002ms 后 860.72，2001ms 后 913.80。释放后继续移动约 448.69 个 UI 本地坐标单位且逐渐减速，确认存在惯性，不能将此数值直接等同于截图像素。观察完成后正常释放本次 GC handles 并卸载脚本，框架 WGC 截图 inertia_before.png / inertia_after.png 留存在 .pytest_tmp/bridge_live。

## 直接调用滚动处理器对照实验

用户将游戏调整到仓库后，运行时枚举得到唯一活动 ScrollRect：Canvas/UICanvas/Depot/Group_Item/ScrollGrid_Item。为避免点击物品，完全不挂接鼠标输入、不发送 PointerDown/Up/Click；在游戏主线程创建 PointerEventData，设置对应 UI raycaster，直接调用 Unity ScrollRect.OnBeginDrag → 逐帧 OnDrag → OnEndDrag。

正向实验仍用 310 像素位移、0.5 秒移动和0.5 秒终点保持。框架 WGC 的 direct_before.png 与 direct_after_ok.png 确认列表向上移动，没有打开详情。这证明仓库滚动控件本身可以处理直接拖动；此前失败发生在普通输入到拖动目标建立/派发这段路径，但具体根因仍未确定。

实验脚本 direct.js 首次因布尔参数表示不匹配在创建阶段失败，修正后正向拖动成功，但 GC handle 清理使用 forEach 直接传 NativeFunction 导致参数数量错误。随后修正为单参数包装，反向实验报告 completed=true、elapsedMs=1004，没有再次发生清理错误。反向完成后 WGC 连续两次报目标客户区无效，游戏进程仍 Responding=True；未取得反向结果截图，不能宣称视觉恢复已确认。

所有 Frida 脚本已卸载、宿主会话退出。失败那次创建的两个 GC handle 未能确认释放，可能保留到游戏进程结束；后续正常实验的 handle 已释放。本轮没有使用道具或修改游戏文件。

此实验是用户指定的对照，不代表将仓库专属对象路径加入通用桥接实现。产物位于 .pytest_tmp/bridge_live/direct.js 和 direct_*.png。

## 后续实际任务参数测试：仓库（2026-09-10）

本节结果收窄下文的早期可行性结论：菜单拖动成功不代表仓库采集拖动兼容。尚未接入正式 controller，因此本次是复现实际任务的输入参数，不是完整执行库存识别任务。

- 参数来自 inventory_pc_actions.py 的 app.drag 与 inventory_items.json：客户区 (1000,620)→(1000,310)，移动 0.5 秒，终点按住 0.5 秒再释放。原型统一转换为 Unity (1000,100)→(1000,410)，按实际帧采样毫秒时间；没有改玩法。
- 桥接点击：主界面头像→个人菜单→仓库成功；仓库材料页签切换成功；道具取消、材料详情空白处关闭成功；使用鼠标点击首页按钮恢复，不使用 Escape。
- 道具页拖动两次失败，材料页拖动一次失败：列表未翻页，打开起点物品详情。没有点击使用或获取途径。
- ProcessDrag 的只读观察记录到位置从 (1000,100) 经 (1000,179)、(1000,258)、(1000,338) 到 (1000,410)，delta 非零；但 pointerDrag 为 null，dragging 始终 false。可确定虚拟位置已进入事件数据，尚未确定为何未建立拖动目标，不能据此断言具体根因。
- 此缺口需要继续检查原有命中、拖动目标选择、控件状态及输入源覆盖；不能以强行设置 pointerDrag 或直接调用仓库滚动控制器作为通用方案的修复。
- 证据在 .pytest_tmp/bridge_live：inventory_before.png、inventory_drag1.png、inventory_drag2.png、materials_click.png、materials_drag.png、materials_closed.png、task_restored.png。实验脚本新增 ProcessDrag 观察与持续时间/终点 hold 支持。

当前验收：实际仓库导航点击通过；实际仓库列表拖动不通过。不能将桥接作为系统鼠标输入的完整替代发布。

日期：2026-09-10；目标为已运行的雷索纳斯 PID 30188。此记录验证最小接管原理，不代表正式服务已完成。

用户要求使用框架截图。结果观察使用仓库 tools/capture_probe.py 与 Windows WGC 后端，客户区图像 1280×720；测试截图和脚本位于 .pytest_tmp/bridge_live。早期曾使用 computer-use 恢复最小化窗口并观察，收到用户要求后停止使用并重置其会话；以下输入测试不使用 computer-use 点击或键盘。

## 方法

以临时 Frida 原型加载到游戏，观察/修改 UnityPlayer 原生 Input 访问器返回值：mousePosition、鼠标 held/down/up、GetKeyDownInt。位置为 Unity 屏幕坐标；按下、移动、抬起根据实际 Time.frameCount 展开。

该原型使用 onLeave 覆盖返回值，并非正式 C++ native replacement；它仍执行原访问器。没有修改玩法 Lua 或直接调用按钮业务函数。只读原帧计数用于简单时序，没有安装正式 PlayerLoop 发布节点，没有验证全部轴和按键。

UI 焦点处理：在 EventSystem.Update 进入时保存 m_HasFocus，原型活动期间暂置为 true，离开时恢复。记录的是修改前焦点。无 OS 聚焦调用、无 SetCursorPos、无 SendInput。

## 观察结果

| 项目 | 结果 |
| --- | --- |
| 挂接/卸载 | 成功获取实际帧计数；每次实验后 script unload、session detach 正常结束 |
| 仅覆盖输入，不处理焦点 | 点击未打开菜单；不能计为成功 |
| 输入覆盖 + 通用焦点作用域 | 虚拟点击客户区头像打开个人菜单 |
| 虚拟 Escape | GetKeyDownInt(27) 的单帧边沿使原返回逻辑关闭菜单 |
| 持续拖动 | 60 帧内虚拟鼠标从 Unity (445,100) 移到 (445,340)，菜单列表向上滚动，显示资料库、行车日志等下方项目 |
| 失焦状态 | 最后一段会话记录到 5385 个原始 unfocusedFrames；未记录 focusedFrames |
| 系统光标 | 成功的点击、Escape、拖动操作前后和状态查询均记录 [2217,789]；原型无移动系统光标代码。不是逐毫秒连续光标轨迹记录 |
| 恢复 | 虚拟 Escape 返回主界面，全部测试拦截卸载，Python 会话退出；游戏 Responding=True，WGC 截图正常 |

## 证据文件

- .pytest_tmp/bridge_live/after_click.png：未处理焦点时，仍为主界面。
- .pytest_tmp/bridge_live/after_focus_click.png：通用焦点作用域开启后，个人菜单打开。
- .pytest_tmp/bridge_live/after_escape.png：虚拟 Escape 后返回主界面。
- .pytest_tmp/bridge_live/drag_before.png、drag_after.png：列表滚动前后。
- .pytest_tmp/bridge_live/restored.png：卸载测试前完成界面恢复后的框架截图。
- .pytest_tmp/bridge_live/probe.py、probe.js：本版本最小实验脚本，含本版本地址，仅用于复现实验，不可作为发布程序。

## 可支持的结论

补充失败记录：拖动后第一次用于恢复界面的 Escape 未在随后的截图中产生返回效果；重新挂接并再次发送后成功返回，已用最终 restored.png 确认，再卸载原型。原因尚未定位，可能涉及当前简化的单帧调度或业务消费时机，不能将 Escape 的成功记录解释为每次均稳定。正式帧快照与完成回执仍是必要工作。

已实际证明：在这个运行版本上，输入访问器返回值接管加通用 EventSystem 焦点作用域，能在失焦时触发点击、滚动列表拖动和 Escape 返回，不必为玩法改写控制器，也不必移动系统鼠标。

未验证：魔方拖动场景、冷缓存首次调用、GetKey 的持续按住/组合键、GetAxis 与命名按钮、正式 PlayerLoop 顺序、所有原生内联消费者、长期稳定性、最小化操控。文本输入明确不在范围内。此次成功不证明不会触发服务端风控或其他检测。

正式方案保留通用输入层路线；必须把实验中的 JS 回调替换为可控的原生实现，完成版本解析、完整输入快照、取消与重连以及未验证能力，再接入专属 plan 服务。
