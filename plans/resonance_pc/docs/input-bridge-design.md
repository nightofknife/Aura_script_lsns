# 雷索纳斯通用输入桥接设计

实施决策以 [通用输入桥接实施方案](input-bridge-implementation-plan.md) 为准。本文保留研究证据和过程中的限制说明；文本输入已移出首版范围。

2026-09-10；已进行静态分析与运行进程的只读缓存检查，尚未实现进程内输入接管或验证操控效果。文本输入按用户要求暂缓。

## 目标与结论

所有桥接服务、宿主、版本解析和配置均放在 `plans/resonance_pc`。上层任务保持现有 click、move_to、drag_to、press_key、key_down/up 等接口，通过模式配置切换系统输入与桥接输入。不为魔方、返回键、家园或战斗新增玩法适配，不调用业务函数代替输入，不重新实现回合规则。

现有客户端可见的多条输入路径汇合到旧版 `UnityEngine.Input`。适合研究的接管层是 IL2CPP 编译后的 Unity 输入读取边界，提供统一虚拟输入状态，使原有 EventSystem、C# 和 xLua 自己处理输入。单纯发送 PointerEventData、替换 Lua 全局 Input 或设置 BaseInput.inputOverride 均覆盖不全。

这不是已发现的官方输入写入 API：它需要进程内改变输入读取行为。静态分析证明了公共读取路径存在，尚不能保证接管点覆盖所有内联、原生和缓存调用。文本事件、焦点处理以及后台更新也需要通用引擎层支持，不能将它们藏在“完全兼容”的承诺中。

## 实际代码证据

来源：`D:/software/soli/Resonance/GameAssembly.dll`、`雷索纳斯_Data/il2cpp_data/Metadata/global-metadata.dat` 及 `雷索纳斯_Data/Patch/Script`。

本次解析了 IL2CPP 24.4/24.5 元数据与函数映射，并检查选定函数的 x64 指令；Lua 使用 XLua 格式 Lua 5.3 字节码。Lua 行号来自字节码调试信息。调用引用扫描用于定位候选调用者；选定函数的指令检查与全量运行覆盖不是一回事。

### Lua 与 UI 的共同输入源

`CSUsing.lua` 顶层指令 37–40 将 `CS.UnityEngine.Input` 赋给全局 `Input`。

`UICubeRogueMain/DragHandle.lua` 18–52 行中的 down/drag 从 Input.mousePosition 读取位置；BindEvents 的回调忽略传入坐标。保持其函数不变，只改变下层读取值，就能保留原来的旋转、声音和拖动状态。

`UIGoBack.lua` 128–173 行先查询 `CS.UnityEngine.Input.GetKeyDown(KeyCode.Escape)`，然后检查网络遮罩、引导、面板和返回按钮。提供虚拟按键边沿后，原有 GoBack 正常消费，不拆分、不复制该函数。

`BaseInput.get_mousePosition` 的原生指令确认调用 `UnityEngine.Input.get_mousePosition`；`BaseInput.GetMouseButtonDown` 转发到同名 Input 方法。因此两条路径可以共享同一输入快照。

### C# 也绕过 Lua 直接读取 Input

引用扫描定位到以下调用者：Battle.BattleUI.UpdateUseCard、MouseManager、HomeCamera、Monopoly.MonopolyInput.OnUpdate、WasteLandCamera.HandleMouseInput、TouchCamera 等直接读取 Unity Input 的鼠标状态；部分调试工具也会读取键盘。不能用 Lua 全局替换覆盖这些消费者。

这同时说明 MouseManager 并不是全游戏唯一输入入口，替换它也不够。虚拟输入须像物理输入一样由所有正常消费者可见，而不是只对某个业务回调有效。

### IL2CPP 输入包装函数

已检查 Input.get_mousePosition、get_mousePosition_Injected、GetMouseButtonDown、GetKeyDownInt：这些包装函数会延迟解析并缓存原生函数指针，然后调用原生实现。

关键实现约束：

- get_mousePosition 和其 `_Injected` 包装路径均能直接进入原生边界；不能假定只替换其中一个命名方法就覆盖另一个。
- KeyCode 与 string 两种 GetKey/GetKeyDown/GetKeyUp 重载均存在。部分公共方法与内部方法共用代码地址，需要按实际地址去重，不能重复接管。
- 仅改变方法元数据可能漏过已经编译好的直接调用；仅改变解析器可能漏过已缓存指针。具体拦截位置必须确认能够覆盖直接调用、反射调用、缓存及内联路径。
- 向量返回与 out 参数有不同 ABI，不能把所有入口都当成普通返回标量的函数。
- 包里有 NativeInputSystem 等引擎类型不能证明业务采用新版 Input System。本次确认的主路径为旧 Input；新系统及纯原生输入消费者仍需单独盘点。

### 焦点处理不是一个属性开关

StandaloneInputModule.Process 与 UpdateModule 的指令直接读取 EventSystem 内的缓存状态，并依据桌面操作系统进行失焦分支。Process 会提前退出；UpdateModule 进入鼠标释放相关路径。检查逻辑存在内联，不能只修改 ShouldIgnoreEventsOnNoFocus 或 Application.isFocused 就宣称解决。

桥接应在通用输入模块边界定义“桥接会话拥有输入焦点”的行为，同时保留真实系统焦点信息。必须覆盖模块更新和派发两个阶段，并确认不重复执行 Process、不重复发送焦点变化回调。具体实现方式留待运行时验证；不按玩法逐个修改焦点判断。

### 文本框还消费另一条事件路径

UnityEngine.UI.InputField.OnUpdateSelected 的反汇编确认循环调用 UnityEngine.Event.PopEvent，随后根据事件类型调用 KeyPressed。仅接管 Input.inputString 或 GetKeyDown 无法保证文本编辑、删除、光标移动、组合键全部生效。

通用方案必须包含一致的键盘/字符事件队列。TMP_InputField 存在独立实现，不能因为支持 uGUI InputField 就认定覆盖 TMP；IME 也需独立验证，但仍属于输入层适配，不属于任务适配。

## 架构

```text
现有任务 / app / controller
    → resonance_pc 专属 controller 路由
    → resonance_pc_input_bridge 服务
    → 本地 IPC → 游戏内命令队列
    → 每帧发布虚拟输入快照 + 通用字符/按键事件
    → Unity Input 读取边界 / 引擎事件队列
    → 原有 EventSystem、C# Update、Lua Update
    → 原有控件、拖动、按键和业务逻辑
```

桥接不额外派发点击或拖动回调。原 EventSystem 仍负责命中、hover、拖动阈值、click eligibility、drop/end 和对象生命周期，避免真实模块与桥接重复执行同一事件。

拟新增文件（尚未实现）：

```text
plans/resonance_pc/
  src/services/resonance_pc_input_bridge_service.py
  src/services/resonance_pc_controller_service.py
  src/bridge/contracts.py
  bridge/host/                  # IL2CPP 宿主与主线程调度
  bridge/input/                 # 输入读取、快照、事件、焦点和轴
  bridge/profiles/              # 游戏/Unity 版本描述
```

不再设 cube_input_adapter、go_back_adapter 或其他玩法专属模块。

项目框架支持同 alias 显式 replace 与契约校验；resonance_pc 的 controller 替换基础 plan controller，依赖桥接服务及 target_runtime，不依赖 controller 自身。保留全部同步、异步、按住上下文和释放接口。

`resonance_pc.input.mode = system | bridge` 为拟定配置，system 保持原路径。bridge 不可用时明确报错，不自动回退系统输入。切换仅在输入空闲且无在途手势时执行，不支持拖动中途热切换。

AppProviderService.focus_with_input 直接调用 target_runtime，绕过 controller。实现需要在 plan 专属 app 门面处理自动化激活需求，避免旧任务重新抢焦点；显式 OS 聚焦与桥接会话就绪应区分。screen/WGC 保持原路径，失焦更新与截图可用性分别确认。

## 统一输入状态与接口语义

每个游戏帧使用同一不可变快照：frame_id、position、pointer_delta、scroll_delta、held_buttons、down_buttons、up_buttons、held_keys、down_keys、up_keys、axes、named_buttons、text_events。

- 在已确认的输入更新阶段之后、首个相关消费者之前发布。包中存在 UpdateInputManager 和 PlayerLoop API，但最终插入位置需实测确认，不能随便放在某个 MonoBehaviour.Update。
- Down/Up 在整帧内可重复读取，不能第一次查询就消费掉；下一帧清边沿，held 持续。
- 同一帧多次 FixedUpdate 不推进命令时间线。状态推进以渲染/输入帧为准。
- 队列里的 down 与 up 不得合并成消费者看不到的瞬时操作。即使 duration=0，完整点击也要给消费链留下可观察边沿。
- 物理键盘鼠标与虚拟输入采用会话独占策略，不简单 OR；否则用户在其他窗口输入会进入游戏。非接管会话保持原路径。
- mousePosition、鼠标按钮、KeyCode.Mouse*、anyKey/anyKeyDown、named buttons 和相关轴应相互一致。

| 现有操作 | 桥接展开 |
| --- | --- |
| move_to / move_relative | 按 duration 在多个游戏帧更新虚拟位置 |
| click | 定位、按钮 down、后续帧 up；clicks/interval 保持接口语义 |
| mouse_down / mouse_up | 变更对应按钮 held 状态及边沿 |
| drag_to | 从虚拟当前位置展开原接口约定的按住、路径、释放序列 |
| press_key | 解析现有键名到游戏 KeyCode，产生 down/held/up |
| key_down / key_up | 支持跨调用长按及组合键，不按键名直接调业务函数 |
| scroll | 转换现有方向与量到 Unity 滚轮单位，下一帧复位 |
| type_text | 通用字符事件和编辑按键事件，与输入快照保持一致 |
| look_delta / look_hold | 更新相对鼠标输入；轴单位、平滑及锁定光标语义需对齐 |
| release_all | 释放本会话 held 状态；不等于任意 UI 手势无副作用取消 |

坐标保持现有客户区截图语义。桥接握手返回实际渲染视口与尺寸，统一映射到 Unity 的坐标方向，包含缩放和留黑边。位置与相对轴分开计算，不能把像素位移直接当成 GetAxis 返回值。

轴映射应来自当前游戏的 InputManager/输入配置，包括 sensitivity、gravity、dead zone、invert 和按钮映射。不能固定把 W 写成 Vertical=1、把 Enter 写成 Submit=true 就称通用兼容。提取配置与核对原生轴行为是实施前必要工作。

魔方继续读取虚拟 mousePosition 并执行原来的 delta * rotSpeed * deltaTime。桥接保留其帧率相关行为，不单独补偿魔方灵敏度；API 兼容不意味着不同帧率下固定拖动距离产生完全相同旋转角。

## 会话、完成与释放

IPC 绑定 PID、启动标识、会话 ID，并限制为本机当前用户。命令带 sequence、deadline、视口和场景 generation；返回 received、applied_frame、completed/rejected/unknown。只在主线程访问 Unity 与 Lua。

click 的完成表示其输入序列已被帧消费，不表示业务通过或服务器确认；现有任务仍通过截图判断结果。超时结果不明时不自动重放点击，sequence 去重避免重复操作。

切换窗口大小或场景后，未执行的旧坐标命令取消。断连后停止新输入并释放持有状态；需要特别说明，正常 up 可能在某些控件触发点击。应另行定义通用 cancel_gesture，通过输入模块清除 click eligibility 后释放；不能把 release_all 承诺为没有业务副作用。目标销毁时不访问旧对象。

宿主只有在不再有在途回调、持有状态和缓存指针引用时才能卸载；故障时可以先退出接管、保持宿主存活，不能贸然卸载仍被调用的代码。重连必须重新握手而非重放旧事件。

## 实施阶段与验收边界

1. 原生输入接口、缓存/内联路径盘点；确认 IL2CPP 加载、ABI 和主线程执行环境。
2. InputManager 配置提取、坐标与帧时间线契约；保留原 controller 方法语义。
3. 实现鼠标与键盘统一快照读取；确认 UI、C# 和 xLua 同帧看到相同状态。
4. 完善通用焦点、文字事件、输入轴和取消机制。
5. 在实际存在的控件与场景上验收，未支持能力明确返回原因，不以任务专属补丁填补覆盖缺口。

游戏由用户启动。本轮读取了该进程的输入函数缓存，没有注入代码或执行操控测试。当前交付为设计、静态证据和运行缓存证据，不能标记为可无缝切换的成品。后续验收要证明旧任务无需修改、系统鼠标不动、无重复点击、组合键与长按一致、失焦输入可用，并核对任务直接调用 target_runtime 的旁路。文本编辑后续再验收。

本地数值检测研究不构成桥接不可检测的保证。本方案会改变输入读取路径，但不修改玩法数值、规则或异常上报。

## 当前版本指纹与研究定位

SHA256：

- GameAssembly.dll：5257B20F1EA21D786B5CF09E3293439D7BEFA9C425DA9522100571C6298ADD69
- UnityPlayer.dll：92CF1F8D45D82CD7E56E4C2AAE7E3FDB45CC83E4547D8D4BAE16FF41EBA72A15
- DragHandle.lua：53A543BB5553571A6CF53E76B7247F3E339C64C40BF63C017D2C2EDCDE283176
- UIGoBack.lua：D19B250E84985B78A102D9ABC840546A25F474C0FAB49F305E1B157FE933B2E8

代表性 GameAssembly RVA（仅作本版本静态证据定位，不是运行时配置）：Input.get_mousePosition 0x27B7E50；BaseInput.get_mousePosition 0x10A4040；StandaloneInputModule.Process 0x10BA050；UpdateModule 0x10BAF70；InputField.OnUpdateSelected 0x16E6B10。运行时不能将这些位置硬编码为跨版本入口。

## 运行进程研究：缓存、内联、调度和轴

检查对象为用户启动的 PID 30188，启动时间 2026-09-10 11:29:52。本次只使用进程查询和 ReadProcessMemory，关闭读取句柄后退出，没有更改目标内存。

### 缓存：已获取运行证据

实际解析后的 Input 原生指针指向已加载的 UnityPlayer.dll。mousePosition 与 mousePosition_Injected 共用同一缓存槽；mouseScrollDelta 的两个包装路径同样共用缓存。

| 输入入口 | 观察结果 |
| --- | --- |
| GetMouseButton / Down / Up | 均已缓存原生实现 |
| mousePosition / mouseScrollDelta / mousePresent | 均已缓存原生实现 |
| GetKeyDownInt | 已缓存原生实现 |
| GetAxisRaw / GetButtonDown | 已缓存原生实现 |
| GetKeyInt / GetKeyString / GetKeyUpInt / GetKeyUpString | 本次观察时未解析 |
| GetAxis / GetButton / GetButtonUp / GetKeyDownString | 本次观察时未解析 |

未解析仅代表本次槽值为零，不能据此判断游戏永久不用这些接口。

设计决策：优先评估接管包装函数最终进入的原生输入访问器，而不是只改元数据、Lua 映射或缓存槽。已缓存调用与未来解析都应进入相同的接管访问器。所有入口需由版本匹配与符号/指令解析获得，并校验其归属模块；不能复制本次地址作为通用配置。对尚未解析入口，要在宿主具备正确运行环境后解析，不能从外部线程随意调用目标函数。

该选择仍需验证原生访问器自身的内联、原生内部调用与其他缓存路径，尚未安装拦截或证明全部覆盖。

### 内联：收敛到原生边界，避免遗漏包装层

mousePosition 的普通包装会直接读取原生缓存指针，并不先调用名为 mousePosition_Injected 的方法。仅拦截 Injected 包装的方案应排除。KeyCode 公共重载和内部包装存在同地址合并，按地址去重后才可安装或撤销接管。

失焦判断已经在 StandaloneInputModule.Process / UpdateModule 中内联，继续沿当前模块的实际执行路径处理，不能仅改变 ShouldIgnoreEventsOnNoFocus。输入访问器接管与焦点兼容属于两个独立引擎层问题。

### 帧调度：明确候选方案，运行顺序尚待宿主观察

包内存在 GetCurrentPlayerLoop、SetPlayerLoop 和 UpdateInputManager。GetCurrentPlayerLoop 的编译实现确实从原生取回当前循环，再转换为 PlayerLoopSystem；不是只有空声明。

候选方案为：在主线程取得当前 PlayerLoop，保留所有原有节点，增加位于原输入更新之后、首个脚本输入消费者之前的快照发布节点。不得使用默认 PlayerLoop 覆盖当前循环，也不得以 OEF 业务循环作为全局输入帧的唯一时钟。

每个发布阶段只推进一次命令队列，使用实际游戏帧标识；Down/Up 和鼠标位移整个输入帧保持不变。不同消费者的读取不能消耗边沿。停止接管时只移除自己添加的节点，不能覆盖其他模块之后修改过的循环。

安装前要观察实际循环及 FixedUpdate/Update/UI/Lua 消费先后。当前只读检查不能调用 GetCurrentPlayerLoop，因此节点存在及静态实现已确认，实际循环顺序、回调生命周期和宿主主线程挂接仍未完成。这是下一阶段原型工作，不应写成调度已经实测通过。

### 输入轴：已从安装资源提取

来源为 globalgamemanagers 的 InputManager 对象，Unity 版本为 `2019.4.40f1c1`。共 18 条轴定义，有重复名称，不能用普通 name→single_axis 字典丢弃配置。

| 名称 | 键盘/鼠标绑定或类型 | sensitivity / gravity / dead |
| --- | --- | --- |
| Horizontal | 左右方向键；A/D；另有手柄 X 轴 | 键盘 3 / 3 / 0.001；手柄 1 / 0 / 0.19 |
| Vertical | 上下方向键；W/S；另有反向手柄 Y 轴 | 键盘 3 / 3 / 0.001；手柄 1 / 0 / 0.19 |
| Fire1 | left ctrl 或 mouse 0；另有 joystick button 0 | 1000 / 1000 / 0.001 |
| Fire2 | left alt 或 mouse 1；另有 joystick button 1 | 1000 / 1000 / 0.001 |
| Fire3 | left shift 或 mouse 2；另有 joystick button 2 | 1000 / 1000 / 0.001 |
| Jump | space；另有 joystick button 3 | 1000 / 1000 / 0.001 |
| Mouse X / Mouse Y / Mouse ScrollWheel | 鼠标移动类型，axis 分别 0/1/2 | 0.1 / 0 / 0 |
| Submit（两条） | return 或 joystick button 0；enter 或 space | 1000 / 1000 / 0.001 |
| Cancel | escape 或 joystick button 1 | 1000 / 1000 / 0.001 |

Horizontal/Vertical 键盘定义的 snap=true；鼠标轴 invert=false。表中浮点数为显示舍入，运行配置应保留原始值。

设计上区分 GetKey 的物理键语义、GetButton 的命名映射、GetAxisRaw 的原始状态和 GetAxis 的平滑状态。按配置处理重复名称、方向反转、snap、dead、sensitivity、gravity。配置值已确定，但鼠标原始单位与 Win32 相对位移对应、重复轴优先规则及平滑时间基准仍需和本版本原生行为核对，不能直接由配置表推导“像素×0.1 永远等于原生轴值”。

PlayerSettings 对象的通用类型树读取失败，未据此读取或断言 runInBackground。InputManager 对象单独解析成功；不将其中一个对象的成功外推到另一个对象。

### 本轮交付与下一步

缓存和轴配置已获得具体运行/资源证据，内联边界已排除若干错误方案，帧调度形成了具体候选方案。尚需宿主观察与原型验证才能解决实际操控；当前没有声称四项均已完成。

下一步最小宿主先只记录主线程帧、循环阶段和输入读取值；随后才提供鼠标/按键快照。暂不接入文本事件，也不修改任何玩法模块。文本输入能力应明确报告 false，避免上层误用。
