# 通用鼠标输入桥接实施方案 V2：交付状态

2026-09-10。已实现首版原生鼠标桥接并完成当前版本实机验证。参数以 [鼠标操作对接契约](input-bridge-contract.md) 为准；构建与启用方式见 [bridge README](../bridge/README.md)，验证范围见 [原生宿主验收记录](input-bridge-native-results.md)。旧 Frida 实验仅作为研究记录保留。

## 1. 范围与接入

桥接服务、版本 profile、C++ 宿主和 loader 位于 `plans/resonance_pc`。通过 manifest 的显式 replace 注册专属 controller/app，现有任务继续使用原方法和参数。框架注册器另有一项通用修正：当前 plan 内预解析的基础服务 FQID 也遵循显式替换链；其他 plan 不受该替换影响。

默认配置保持原系统输入：

```yaml
resonance_pc:
  input:
    mode: system
    stop_inertia: true
```

将 mode 改为 bridge 即启用。存在按住状态或在途操作时拒绝切换；失败不回退 SendInput。截图继续使用 screen/WGC。`focus_with_input()` 只准备桥接，显式 `focus()` 仍按调用者要求聚焦窗口。

首版支持定位、相对位置移动、点击、按住/释放、完整拖动、滚轮、释放全部和取消，以及对应异步入口。键盘、文本和 `look_*` 接口明确返回未支持。

## 2. 参数与返回值

Python 侧沿用 runtime_config/provider_options，完成坐标裁剪、整数转换、次数、秒单位时长、缺失坐标和默认值解析。区分动作层与服务层默认值，保留 None 与显式 0、点击后等待和拖动终点保持。

app/controller 成功返回 None；现有动作层继续返回 True/False。完整 `drag/drag_async/drag_to/drag_to_async` 增加 keyword-only `stop_inertia=None`，默认读取配置。显式 False 保留 ScrollRect 惯性。

## 3. 原生输入与逐帧执行

宿主使用 MinHook 拦截解析出的 UnityPlayer 最终 Input 访问器，核对 IL2CPP 包装缓存和最终入口，并按地址去重。profile 通过 GameAssembly、UnityPlayer 和 metadata 的 SHA256 绑定版本，不包含玩法对象路径。

EventSystem.Update 提供主线程初始化入口。宿主解析当前 PlayerLoop，在 EarlyUpdate.UpdateInputManager 后发布输入，在根循环尾部确认帧完成。当前 Unity 版本的内部扁平循环使用后代节点计数，回调字段指向函数指针槽；实现按该 ABI 检查结构后安装节点。

鼠标读取共享帧快照，读取次数不推进时间线。点击 down/up 分属可消费帧；完成回执表示输入执行完成，不保证业务结果。桥接持有会话期间隔离原物理输入；首版不开放键盘模拟能力。

原 EventSystem 处理点击。完整拖动由宿主派发到通用解析器找到的处理器，期间抑制原模块重复派发。解析器使用有序 RaycastAll、父链 IDragHandler，以及同 Canvas 分支、viewport 包含和唯一候选约束下的 ScrollRect 兜底。目标缺失、遮挡或歧义时返回错误，不穿透弹窗猜测列表。

ScrollRect 拖动使用初始化、开始、拖动、结束事件，并按参数调用 StopMovement。其他 IDragHandler 使用接口分派。取消清除捕获与点击资格，不额外派发可能触发业务行为的 PointerUp；release_all 保留普通释放语义。

## 4. 焦点、坐标与生命周期

宿主在 EventSystem 更新作用域内保存、覆盖并恢复逻辑焦点；会话负责保存和恢复后台运行/光标相关状态，不激活系统窗口。当前 profile 只接受 1:1 客户区与渲染尺寸。完整拖动还核对场景、对象有效性和 viewport，变化后停止执行。

本地命名管道限制当前用户，绑定实际客户端进程、目标 PID/创建时间、会话和序号。消息大小上限 64 KiB；有界历史缓存保留完成结果，拒绝过期序号。未知结果不自动重放。客户端进程死亡触发取消和输入恢复。

会话关闭后宿主保持 inert，保留回调代码直到游戏退出，不强行卸载 DLL。网络工作线程不承担 Unity 操作；进程退出时避开托管对象回收和 IL2CPP 线程等待，正常窗口关闭已实测。

## 5. 交付文件与边界

- `src/bridge/client.py`：管道客户端与协议错误。
- `src/services/input_bridge_service.py`、`bridge_controller_service.py`、`bridge_app_service.py`：归一化、替换服务、完整手势和异步取消。
- `bridge/host`、`bridge/loader`、`bridge/profiles`、`bridge/tools/build.ps1`：源码、版本配置和构建入口。
- `assets/input_bridge`：已编译 DLL、loader、profile、第三方许可证；既有发布组装器会收录这些运行资产。

当前验收覆盖多个列表、弹窗遮挡和标准 Slider，未穷尽全部游戏场景。游戏更新、最小化、非 1:1 渲染、命名轴和相对视角不在首版兼容承诺内。新增版本需重新核对 ABI 和实机行为，不能只替换指纹。
