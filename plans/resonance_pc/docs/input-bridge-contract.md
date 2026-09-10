# 鼠标操作对接契约

2026-09-10；状态：首版原生鼠标桥接已实现，验证范围见 [原生宿主验收记录](input-bridge-native-results.md)。适用 resonance_pc 的 Windows/SendInput → bridge 切换，不覆盖 MuMu 或 window_message 的内部行为。本文件优先于此前方案中笼统的“参数一致”描述。

## 1. 来源与职责

核对来源（仓库相对路径）：

- `plans/aura_base/src/actions/input_actions.py`：任务动作参数与返回值。
- `plans/aura_base/src/services/app_provider_service.py`、`controller_service.py`：服务参数、拆分手势、异步与释放。
- `plans/aura_base/src/platform/windows/input_backends.py`：实际参数归一化、移动轨迹、次数、间隔与滚轮。
- `plans/aura_base/src/platform/runtime_config.py`、`look_math.py`、`services/input_mapping_service.py`：配置和映射展开。
- `plans/resonance_pc/config.yaml`、`data/meta/inventory_items.json`、`src/actions/inventory_pc_actions.py`：本 plan 覆盖值及现有任务参数。

动作层保留返回 True/False 的语义；app/controller 成功仍返回 None；详细 RPC 回执只供内部调度与日志使用。不得把操作方法改成返回命令字典。

专属 app/controller 保留所有原有参数名称、位置参数顺序和同步/异步入口。Python 侧归一化一次并保留客户区坐标，宿主负责坐标映射、逐帧执行和 Unity 对象访问。不在两端重复缩放、重复等待或重复展开手势。

## 2. 默认值：按入口区分

| 参数 | 动作层不传参数 | app/controller 不传参数 | 本 plan 最终配置 |
| --- | --- | --- | --- |
| click.interval | 0.1 秒 | None → 配置 key_interval_ms | 40ms，即 0.04 秒 |
| double_click | 固定 left、clicks=2、interval=0.05 | 无同名 app 方法，使用 click | 每次点击仍有后置等待 |
| right_click | right、clicks=1，经过动作 click 的 interval=0.1 | 使用 click(button='right') | 单击无两次间隔等待 |
| move_to.duration | 0.25 秒 | None → mouse_move_duration_ms | 120ms，即 0.12 秒 |
| mouse_move_relative.duration | 0.2 秒 | None → mouse_move_duration_ms | 0.12 秒 |
| drag.duration | 0.5 秒 | app.drag/drag_to 为 None → 配置 | 0.12 秒 |
| hold_before_release_sec | 0.0 秒 | app.drag 为 0.0；drag_to 无此参数 | 仓库采集显式传 0.5 秒 |
| click_post_delay | 无公共参数 | 每次点击后由后端等待 | 本 plan 50ms；基础默认 30ms |
| look_hold.tick_ms | 16ms | None → look.tick_ms | 默认 16ms |

禁止在桥接端统一套用动作层默认值。`None` 是读取配置，显式 `0` 是零时长/零间隔。`duration` 与 `interval` 的有限负数经 max(float(value),0) 归零。hold 时间同样非负化。

配置从现有 resolve_runtime_config/provider_options 获取，再应用当前后端的解析规则；不另造一套 profile 优先级。运行配置的 look 正数校验与后端内部防御默认值不可混淆。

## 3. 坐标、按钮与状态

| 项目 | 当前实现 | bridge 契约 |
| --- | --- | --- |
| 绝对坐标 | 客户区坐标，int 转换向零截断 | 不改变调用方坐标空间 |
| 完整 x/y | int 后裁剪到 [0,W-1]、[0,H-1] | 相同规则；使用当前有效视口尺寸 |
| 缺少任一坐标 | 两个参数都不用；采用后端缓存坐标，否则客户区中心 | 相同选择规则，采用虚拟指针缓存；不能用已传入的另一轴补齐 |
| 初始指针 | 无缓存时 int(W/2),int(H/2) | 会话初始位置为中心，不从系统光标初始化 |
| 相对移动 | 缓存位置/中心 + int(dx/dy)，再裁剪 | 相同；不是读取真实鼠标，不是相机轴 |
| 系统移动起点 | SendInput.move_to 的插值起点取实际 OS 光标 | 有意差异：始终取虚拟指针位置，避免用户移动鼠标干扰任务 |
| 按钮 | str(button or 'left').strip().lower()；只接受 left/right/middle | 对应 Unity Left=0、Right=1、Middle=2；不新增 l/r/m 或 XBUTTON 别名 |
| None/空按钮 | None、空串退回 left；纯空白字符串去空白后报错 | 保留这一差别 |
| 窗口变化 | 已有缓存缺省路径不重新裁剪，完整坐标路径裁剪 | 有意差异：旧 generation 手势取消；空闲缓存在新尺寸下重新裁剪 |

宿主使用客户区和实际 Unity 渲染范围建立唯一变换。对整个客户区对应渲染尺寸 Uw×Uh 的情况，采用 ux=x×Uw/W、uy=(H-y)×Uh/H；1280×720 时 (1000,620) 对应 (1000,100)。不加隐含像素中心偏移、不在 Python 先翻转一次。

若图像为局部裁剪，调用方识别层先按原框架约定还原为完整客户区坐标，宿主不猜裁剪偏移。遇到留黑边、子视口或无法确定的尺寸关系，由视口描述明确变换；未匹配时拒绝执行，不猜比例。持续手势执行期间视口 generation 固定。

## 4. 操作映射表

### 点击与移动

| 入口 | 参数归一化与当前行为 | bridge 内部操作与完成条件 |
| --- | --- | --- |
| click(x=None,y=None,button='left',clicks=1,interval=None) | 解析完整坐标或缓存；n=max(int(clicks),1)；先 move_to(duration=0)；每次 down/up 后等待 click_post_delay；仅相邻两次间再等 interval | 单个 Click 命令，宿主逐次执行；每次 down/up 跨可观察输入阶段；最后一次 post delay 结束后返回 |
| app.click_async(x,y,...) | 当前 x/y 必填且 int(None) 失败；controller.click_async 则允许缺省 | 两层签名分别保留，不能把 app 异步缺省规则悄悄放宽；await 与同步具有相同完成语义 |
| move_to(x,y,duration=None) | int 与裁剪；D>0 时 N=max(int(D/0.02),1)，线性插值，坐标使用 Python round 后 int；每一步后 sleep(D/N)，包括最后一步 | MoveAbsolute：归一化 D 和目标；按同样线性、整数舍入轨迹生成逻辑采样，映射到游戏帧；等待 D 和最终消费帧，不能刚定位到终点就提前返回 |
| move_relative(dx,dy,duration=None) | 从缓存/中心累加 dx/dy，再调用 move_to | MoveRelative：在串行输入队列中解析起点，防止同时排队的相对命令读取同一旧位置 |
| mouse_down(button='left') | 发 down；将按钮记入 held 集合；不会定位 OS 光标 | ButtonDown：当前虚拟位置；原生消费者看见一次边沿后返回，held 持续到 up |
| mouse_up(button='left') | 即使未记为 held 仍发 up，再 discard | ButtonUp：保留无条件发送释放意图；不是增加一次点击；未 held 时不制造 down |

clicks=0、负值均至少点击一次。interval 不是 down 与 up 间的按住时长，也不是两次 down 的总周期。n 次点击的显式等待总和为 n×post_delay+(n-1)×interval，另加实际输入消费帧等待。

重复 ButtonDown/Up 不使用引用计数，最终 held 仍为集合。完整 Click/Drag 与现有 held 手势冲突时，bridge 在产生副作用前报 input_gesture_busy，避免悄悄释放调用方持有的按钮；这是新增的明确错误，不伪装成当前后端已有保证。

### 拖动与停止惯性

| 入口 | 当前执行顺序 | bridge 契约 |
| --- | --- | --- |
| app.drag(start_x,start_y,end_x,end_y,button='left',duration=None,hold_before_release_sec=0.0) | 起点 move_to(0) → mouse_down → move_to(end,D) → 等待 H → finally mouse_up | 一个完整 Drag 命令，携带起点/终点/D/H；直接拖动模式不再执行这组底层鼠标调用，防止重复派发 |
| app.drag_async(...) | 同样顺序，移动 await、hold 使用 asyncio.sleep | 一个完整异步 Drag 命令；取消进入手势取消/清理，不只停止 Python 等待 |
| controller/后端 drag_to(x,y,button='left',duration=None) | mouse_down → move_to(end,D) → finally mouse_up，没有起点定位和 H | 从虚拟当前位置开始完整 Drag，H=0；接管同步与异步两个入口 |
| mouse_down → move_to → mouse_up | 调用者分段提供动作；按下时不知道未来是否拖动 | 保留低层状态语义，不承诺与预先知道意图的完整 Drag 相同；不得追溯撤销已经发生的按下副作用 |

桥接的 Drag 参数新增 keyword-only `stop_inertia: bool | None = None`，同时提供给专属 app.drag/drag_async 与 controller.drag_to/drag_to_async。None 使用 `resonance_pc.input.stop_inertia`，默认 true；显式 false 保留惯性。旧位置参数不移动。既有基础动作无需新增参数即可继承 plan 默认值；未来若需要 YAML 单次覆盖，必须另行暴露参数，不能宣称当前动作已接受它。

ScrollRect 完整 Drag 顺序为初始化 → BeginDrag → 按帧 Drag → 保持终点 H → EndDrag → 如开启则 StopMovement。不发物品点击。其他标准拖动处理器按实施方案派发；非 ScrollRect 不调用 StopMovement，stop_inertia 对其无效且在诊断中标记 not_applicable，不臆造通用停止函数。

H 是调用者显式参数，不能因为 StopMovement 有效而自动删掉。Move 采样与 hold 只在宿主执行一份，Python 端不额外 sleep。D=0 仍须完成有效 begin/最终 drag/end 阶段；起终点相同也不降级为 click。

首次默认停止惯性是用户选定的有意行为变化，不是完全复刻 SendInput。默认只保证清零惯性速度，边界弹性回弹仍按原控件行为进行。完整 Drag 结束后缓存更新为归一化终点，而不是列表 content 坐标。

### 滚轮

| 层级 | 当前规则 | bridge 映射 |
| --- | --- | --- |
| 动作 scroll(direction,amount) | direction 用 str(direction or '').lower()；只接受 up/down，不 strip；非法方向返回 False 且不调用 app | 原样保留动作验证 |
| app/controller scroll(amount,direction='down') | app 对 amount 做 int | 保留参数顺序，不能与动作层顺序混淆 |
| SendInput 后端 | ticks=max(abs(int(amount)),1)；direction 经 str(direction or 'down').lower()，恰好 down 才为负，否则为正 | 客户端归一化为 signed_detents；下=-ticks，上=+ticks，保留直接调用服务时的历史方向规则 |
| 单位 | OS wheel_delta=120×signed_detents | 内部传 detents 而非像素；Unity mouseScrollDelta.y 的接口目标为 signed_detents，ScrollRect 自己应用 scrollSensitivity，不把 120 再乘到列表位移 |

amount=0 也滚一格，负数只取绝对值，最终方向由 direction 决定。当前 API 没有滚轮坐标参数：bridge 使用虚拟指针位置命中，而非系统光标。首版的 wheel 映射须实测一格和多格后才声明支持；配置中的 Mouse ScrollWheel 轴 sensitivity≈0.1 不能误用为 mouseScrollDelta 的系数。未通过单位校准时报 input_capability_unsupported，不退回系统滚轮。

## 5. 相对视角：契约完整，首版不启用

look 与绝对鼠标移动不是同一空间。当前 look 不更新后端 `_cursor_position`；不得用 move_relative 替代。

归一化公式沿用后端：先对 dy 应用 invert_y；各轴乘 scale；Python round 后 int；原始非零但舍入为零时取对应 ±1；最后裁剪至 ±max_delta_per_tick。默认 scale=1、invert_y=false、base_delta=24、max_delta_per_tick=96。

| 操作 | 必须保留的展开规则 |
| --- | --- |
| look_delta(dx,dy) | 归一化一次；两轴都零则无输入；非零只发一个相对移动事件，不把单位直接视为 Unity 轴值 |
| look_hold(vx,vy,*,duration_ms,tick_ms=None) | int(duration_ms)>0；tick>0；vx/vy 为有限 [-1,1]；每 tick 的原值为 base_delta×strength，再套上述量化 |
| look_hold 时序 | N=max(ceil(duration_ms/tick_ms),1)；第一个事件立即发；只在相邻事件间等待 tick，末事件后不额外等一个 tick；零向量校验后立即返回 |
| look_direction(direction,strength=.4,duration_ms=200,tick_ms=16) | 方向去空白并小写；left=(-s,0)、right=(s,0)、up=(0,-s)、down=(0,s)；s∈[0,1]；交给 look_hold；不能提前再反转 Y |
| look_sweep_horizontal(total_dx,chunk_dx=24,step_delay_ms=16) | total=0 立即 True；chunk=0 报 look_delta_invalid；delay<0 报 look_tick_invalid；按 abs(chunk) 拆分带 total 符号的 delta，逐块独立量化，每块间等待 delay |

例如 duration_ms=250、tick=16 时 N=16，现有显式等待是 15×16=240ms，不是强制等待250ms。每个逻辑 tick 的量化和限幅必须先发生，再考虑低帧率合并；不能将多 tick 原始位移相加后只限幅一次。

首版 `relative_look=false`，非空请求报 input_capability_unsupported，不发送任何相机或鼠标替代操作。零向量/零 total 的既有动作层 no-op 仍保留。上述契约是后续能力接入依据，不代表已实测 Unity 轴与 OS 相对输入单位等价。

## 6. 输入映射、复合操作与释放

InputMappingService 的 mouse_button：button 默认 left，先 strip/lower，再用 or 'left'，因此该入口的纯空白按钮也退回 left；clicks 默认1且至少1；interval 默认显式0.0，不能落回40ms。press/tap→app.controller.click；hold→app.controller.mouse_down；release→app.controller.mouse_up，实际绕过 app 方法，专属 controller 必须接住。映射服务返回原有包含 ok、phase、type、action_name、binding 的字典，不由桥接改形状。

look 映射的 release 是 no-op；dx/dy 分支先于 direction 分支；direction 分支默认 duration=200ms、tick=16ms，映射层将两者至少设为1。此归一化与服务直接调用的严格正数校验不同，不能合并。

图像/OCR 点击和拖动仍由现有识别服务算坐标，再走 app；桥接不重新 OCR，也不接受模板偏移作为 Unity 世界坐标。

| 释放接口 | 行为 |
| --- | --- |
| controller.release_mouse() | 遍历本地 held 鼠标集合并逐个 mouse_up；有异常按现有调用传播 |
| controller.release_key() | 保留接口；鼠标首版无虚拟键盘 held，清理无事可做时成功 |
| app.release_all()/release_all_keys() | 两者当前都调用 controller.release_all，名称含 keys 的也会释放鼠标，不能缩窄行为 |
| 后端 release_all()/close() | 尽力释放，现实现吞掉单项发送错误后清空集合；bridge 同样可重复清理，记录不确定结果、不重放历史点击 |
| cancel_input()（新增） | 取消队列并按通用手势取消规则清除点击资格/结束拖动；不是原有 release_all 的别名 |

当前 app/controller 没有 hold_mouse 方法；不得声称已有此上下文接口。实际存在的是 mouse_down/up 和键盘 hold_key/hold_key_async。

键盘和 type_text 首版未支持，统一 input_capability_unsupported；保留原方法签名，system 分支保持现状。

## 7. 时序、异常、能力及有意差异

- 所有数值先在原入口按 int/float 归一化。无法转换的类型保留 TypeError/ValueError；超出 int 表示等情况不悄悄截断。桥接额外拒绝 NaN/Infinity 时长、坐标与增益，使用 input_parameter_invalid；此为新增防御，不声称原后端一致拒绝。
- unsupported_mouse_button、look_duration_invalid、look_tick_invalid、look_strength_invalid 等已明确的错误码保留。动作 scroll 的非法方向仍返回 False。参数失败不得在桥接先移动鼠标再报错；原后端部分错误发生在定位之后，这种副作用不复制。
- 成功同步/await 返回必须在最后必要的游戏消费阶段和显式等待之后。帧等待允许超过调用者时长，不能缩短明示 hold/post delay。游戏帧数不是“完成”的唯一回执，需区分 queued/applied/completed/unknown。
- 点击按下与松开分成可消费帧，是相对 OS 连续 down/up 的必要时序差异；不能承诺两种后端毫秒耗时完全相等。
- 低帧率时普通位置可合并中间采样，保留终点；按下/松开和手势边界不可折叠。Python 的20ms采样公式用于轨迹契约，不要求游戏一帧执行多次重复拖动回调。
- mouse_down、移动和 mouse_up 的状态跨调用保存。复杂手势串行且不可被其他完整 click/drag 插入；异步取消必须请求清理后再向调用方传播取消。
- focus_before_input 在 system 模式仍表示 OS 聚焦；bridge 模式表示会话输入就绪与逻辑焦点，不调用真实激活。app.focus() 保留显式 OS 聚焦语义；focus_with_input 在专属门面改为桥接就绪加 max(int(click_delay×1000),0)ms 等待，返回 bool，不偷偷发激活点击。这是有意模式差异。
- 不扩大 require_visible 的承诺；失焦/遮挡与最小化分开。无有效客户区、过期视口或目标进程更换时拒绝旧手势。
- 能力至少区分 absolute_pointer、direct_drag、scroll、relative_look、keyboard、text_input、background_input。背景能力只指已验证的失焦/遮挡，不包含最小化。所有实验成功项也必须经过正式实现验收才能置 true。
- `stop_inertia=True` 在 system 模式没有直接 StopMovement 能力：默认 None 不改变 system 行为；调用者显式要求 true 时应报不支持，不能忽略参数假装已停止。

## 8. 编码前后的对照用例（本轮未运行测试）

| 输入 | 预期规范化/结果 |
| --- | --- |
| app.click(100,None)；无缓存，1280×720 | 使用中心 (640,360)，不补齐 y 并保留 x=100 |
| app.click(-8,900,clicks=0)；1280×720 | 裁剪 (0,719)，执行1次，等待本 plan 的50ms post delay |
| app.click(10,20,clicks=2) | 间隔40ms，两次各50ms post；显式等待共140ms |
| 动作 click(10,20,clicks=2) | 间隔100ms；显式等待共200ms |
| double_click(10,20) | 间隔50ms；显式等待共150ms，不手工调用两次业务 onClick |
| move_to(10,20) 的动作与 app 入口 | 分别250ms和120ms；显式 duration=0 均是零逻辑移动时长 |
| app.drag(1000,620,1000,310,duration=.5,hold_before_release_sec=.5) | Unity 端对应 (1000,100)→(1000,410)，500ms移动+500ms保持，再结束并默认停止 ScrollRect 惯性 |
| scroll(amount=0,direction='down') | -1 detent，不是 no-op |
| scroll(amount=-3,direction='up') | +3 detents，OS语义等价 wheel_delta=+360 |
| 动作 scroll(direction='sideways',amount=1) | False，无宿主命令；直接 app 调用历史规则不同 |
| button=' LEFT ' 与 button='l' | 前者left；后者unsupported_mouse_button |
| look_delta(140,0)，默认配置 | 后续能力目标是(+96,0)，首版报不支持 |
| look_hold(.45,0,duration_ms=250,tick_ms=16) | 后续每tick(+11,0)，16次，间隔显式等待240ms；首版报不支持 |
| app.release_all_keys() | 仍清理鼠标held状态 |
| 异步 drag 在终点 hold 时取消 | 清理手势、释放自己持有的资源，不发送额外click，不返回正常成功 |

验收必须覆盖以上入口差异、零/缺省/负值、连续相对移动和异步取消。验收清单属于通用接口检查，不添加玩法专属测试。本文只完成对接契约，未修改输入运行逻辑、配置默认值或游戏进程。
