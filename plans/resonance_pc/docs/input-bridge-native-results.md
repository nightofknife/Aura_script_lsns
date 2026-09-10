# 原生鼠标桥接验收记录

2026-09-10；Windows x64，Unity 2019.4.40f1c1，1280×720 客户区/渲染尺寸。三项模块指纹见 `bridge/profiles/unity-2019.4.40f1c1.json`。验收使用真实 plan app/controller 服务和编译后的 DLL/loader；截图使用框架 WGC。Frida 只参与前期只读 ABI 诊断，不属于运行依赖。

## 已验证

| 项目 | 实机结果 |
| --- | --- |
| 自动加载与重连 | 首次操作加载宿主并等待 ready；重启游戏后可重新加载、执行输入 |
| 后台点击 | 可进入主界面、菜单、仓库；检查时前台属于其他进程，未由桥接移动系统光标 |
| 通用完整拖动 | 菜单列表和仓库列表可拖动；仓库原参数 duration=0.5、hold=0.5 重复拖动未弹出物品窗口 |
| 惯性开关 | stop_inertia=True、hold=0 的列表在两秒后基本静止；False 保留滚动惯性 |
| 非列表处理器 | 标准音量 Slider 可通过同一 app.drag 调整，验收后恢复原值 |
| 遮挡处理 | 物品弹窗背景区域返回 drag_target_missing，没有拖动后方列表；关闭弹窗，未使用物品 |
| 异步取消 | 两秒拖动在约 0.15 秒取消，清理后抛出 CancelledError，held 为空，未弹出物品窗口 |
| 底层组合 | move_to 后连续 mouse_down/mouse_up 可点击；长按仍触发游戏自己的长按判定 |
| 滚轮 | down/amount=3 产生方向正确的列表位移；未完成对系统后端全量幅值标定 |
| 客户端死亡 | 客户端按住中键后退出，宿主回收会话；新客户端可连接和关闭 |
| 正常退出 | 修正线程附着和析构问题后，两次窗口关闭均正常退出 |

惯性检查使用相同列表区域的截图差异：停止惯性时两秒前后平均绝对差约 0.179，保留惯性时约 50.4。图中含局部动画，该数值只用于这次行为对照。

## 开发中发现并修正

- Unity 内部 PlayerLoop 节点使用后代计数和回调指针槽，初版结构假设导致初始化失败；按实测 ABI 修正并加入边界检查。
- GetComponent 重载和接口分派需要元数据类型核对；修正后列表和 Slider 均可执行。
- 工作线程保留 IL2CPP 附着、进程静态析构释放托管句柄会阻塞游戏退出；工作线程完成解析后分离，运行时采用进程生命期持有。
- 完成命令历史原本会在长会话达到容量后阻塞；改为淘汰最旧完成项并保留序号下界，不淘汰在途操作，取消和关闭不受历史容量限制。
- 任务预解析的基础服务 FQID 绕过替换链；修正通用注册器，仅在声明替换的当前 plan 内解析到专属实现。

## 回归与发布检查

在仓库根目录运行以下通用用例，共 13 项通过：

```text
tests/unit/test_plan_service_replacement.py
tests/smoke/test_package_discovery_smoke.py
tests/smoke/test_runner_smoke.py
tests/contracts/test_resonance_pc_framework_action_reuse.py
tests/contracts/test_async_plan_action_safety.py
```

现有发布文件选择器和校验器通过；选中宿主 DLL、loader、profile 和许可证，未选中开发构建工具。最终 C++ 构建成功。运行依赖为 Windows/UCRT，不需要 Frida 或额外编译器 DLL。测试产物、截图和构建缓存存放在仓库 `.pytest_tmp`。

## 未覆盖与使用限制

未逐一运行全部既有任务，未对所有场景、重叠候选、运行中 resize 和场景卸载组合做穷举。已检查服务替换，并以真实 app API 验证输入链路。profile 仅声明当前指纹的绝对鼠标、直接拖动、滚轮和后台输入能力。

首版拒绝键盘、文本和 look 输入；不承诺最小化或非 1:1 渲染。版本不匹配、拖动目标不明确和失去帧推进均应显式失败，不回退系统输入。回执只表示输入已执行，调用方仍需截图或业务状态确认任务结果。

默认仍为 `resonance_pc.input.mode: system`。启用步骤见 [bridge README](../bridge/README.md)。
