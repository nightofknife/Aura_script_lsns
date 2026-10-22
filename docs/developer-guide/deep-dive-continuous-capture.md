# 深潜独立连续采集工具

日期：2026-10-22。实现已写入，完成只读代码审查；依当前任务要求，未运行测试、未启动录制器、未进行实机采集。首次实机仍需确认本机WGC回调速率、写盘吞吐和输入观察情况。

入口：`tools/deep_dive_continuous_capture.py`。内部实现：`tools/_deep_dive_capture_store.py`、`tools/_deep_dive_capture_input.py`。不导入深潜识别action，不派发正式扫描任务，不修改/替换现有识别函数，不输出目标识别成功。

## 保存什么

```text
logs/deep_dive_capture/<新会话名>/
  owner.json                  # 本次拥有者PID、run id、启动时间
  manifest.json               # 窗口绑定、坐标、采集/输入参数、时钟语义、源码hash
  source/                     # 本工具、相关采集/输入实现及盘面配置的冻结副本
  frames/00000000.png ...      # WGC回调的原始像素，无缩放、无裁剪、无叠字
  frames.jsonl                # 每帧来源、回调时间、间隔、client ROI、PNG hash、写盘时间
  timeline.jsonl              # 输入原语、OS观察、前台变化、命令、人工标记及生命周期
  commands/<id>.json          # 原子发布的原始控制命令
  results/<id>.json           # 命令结果：success/rejected/interrupted/failed
  ready.json                  # 启动阶段完成；不表示采集结束或识别成功
  status.json                 # 运行心跳/队列情况；退出后改为终态
  STOP.json                   # 外部停止请求；不排在普通命令之后
  summary.json                # 完整性、失败原因、丢帧/队列/排空及资源关闭结果
  temp/                       # 本进程TEMP/TMP/TMPDIR
```

输出必须位于仓库 `logs/deep_dive_capture/` 或 `.pytest_tmp/` 下的新子目录；拒绝覆盖已有会话。

PNG保留WGC的原生BGR/BGRA像素。若捕获含非客户区，原图仍保留；逐帧 `client_roi=[x,y,1280,720]` 指明客户区，manifest记录固定屏幕坐标。不要把原生全窗PNG直接当1280×720客户区坐标。没有可靠边框映射时停止，不猜裁剪位置。

## 采集与时间语义

- 直接接收工具私有WGC session的帧回调，回调复制原生buffer后放入有界队列；不轮询“最新帧”替代中间帧。
- 单独线程无损编码PNG、写索引。默认请求约30fps（WGC minimum interval为34ms）；这不是固定帧率保证。保存所有实际接收的回调，不补帧，不因像素相同丢弃。
- `callback_monotonic_ns`为本工具进入帧处理时的单调时间，不是游戏渲染时间。不同进程的提交时间来自本机同一单调时钟；UTC只便于查目录，不能用于运动间隔计算。
- 输入原语记录API调用前后时间，鼠标轨迹拆成约20ms的显式零时长移动，避免后端内部插值未记入日志。
- 旁路LL hooks记录**目标窗口处于前台时**的鼠标/键盘事件，保留屏幕坐标、按钮/滚轮、VK/scan、flags和OS事件时间，不解码输入文字。WinEvent记录离开/返回游戏的前台窗口句柄，不读取其他应用内容。
- 文件行按写入顺序排列，不保证不同生产线程的时间戳严格递增。分析时按 `monotonic_ns` / `callback_monotonic_ns` 对齐；保留journal sequence以追踪原写入顺序。
- hook看到事件不证明游戏已经消费；`injected`标志不能确定注入进程，`SetCursorPos`也不能仅靠该标志认定为人工输入。分析时结合工具调用区间。Windows可能合并/遗漏底层事件，LL hook自身不能证明OS层零丢事件。

因此，“完整”指本工具声明的记录窗口内接收的WGC回调均保存、记录队列未丢项且关闭确认；不宣称捕获游戏全部渲染帧，也不宣称观测到每个物理设备报告。完整工具输入原语和旁路事件同时保留，避免只靠其中一条时间线。

## 启动与控制（未来实机时执行）

在仓库根目录、项目Python环境中执行以下命令。录制前准备普通深潜盘面与1280×720客户区，保持窗口位置、尺寸不变，暂停其他游戏自动化。工具不会启动游戏、自动调整尺寸、自动进入盘面或自动reset。

第一个终端持续运行：

```powershell
python tools/deep_dive_continuous_capture.py record --session logs/deep_dive_capture/board-a-forward-001 --enable-input
```

默认使用 `resonance_pc` 的窗口选择配置，也可加 `--hwnd <句柄>` 精确绑定。它只拥有自己的WGC session、输入后端和hooks；同一HWND的两个本工具实例通过命名mutex互斥，不修改共享配置。不同任务的生产自动化不受该mutex约束，所以采集期间不要并行运行。

不带 `--enable-input` 时只记录，仍能观察游戏前台的手动输入；focus/drag/click命令会被拒绝。启动本身始终不产生聚焦或游戏输入。

看到ready后，在第二个终端逐条发布命令，并查看返回的 `results/<id>.json`；`queued`只表示已发布，不表示执行完成：

```powershell
python tools/deep_dive_continuous_capture.py status --session logs/deep_dive_capture/board-a-forward-001
python tools/deep_dive_continuous_capture.py send --session logs/deep_dive_capture/board-a-forward-001 --op mark --label baseline
python tools/deep_dive_continuous_capture.py send --session logs/deep_dive_capture/board-a-forward-001 --op wait --seconds 5
python tools/deep_dive_continuous_capture.py send --session logs/deep_dive_capture/board-a-forward-001 --op focus
python tools/deep_dive_continuous_capture.py send --session logs/deep_dive_capture/board-a-forward-001 --op drag --dy -20 --duration 0.5 --after 4 --label approach-a
python tools/deep_dive_continuous_capture.py send --session logs/deep_dive_capture/board-a-forward-001 --op drag --dy 20 --duration 0.5 --after 4 --label reverse-a
```

默认拖动从客户区 `(640,480)` 起，`dx=0`，dy限制在±160px且终点必须位于客户区；可显式指定 `--x/--y`。输入前要求游戏已处于前台、无已按住的鼠标按钮及Shift/Ctrl/Alt；拖动中失去前台即中止并释放。focus是独立命令，不在每个输入内部悄悄抢回焦点。

需要reset时，先mark，再用已目视确认的按钮坐标发送click，或由人工点击并利用旁路日志记录；本工具没有自动寻找reset按钮的识别逻辑。

```powershell
python tools/deep_dive_continuous_capture.py send --session logs/deep_dive_capture/board-a-forward-001 --op mark --label before-reset
# 下面的坐标必须按当时原图确认；不要直接复制占位值执行。
# python tools/deep_dive_continuous_capture.py send --session logs/deep_dive_capture/board-a-forward-001 --op click --x <reset-x> --y <reset-y> --after 5
```

`--after`仅控制松手后继续录多久，不断言已经停稳。`mark`仅是人工注释，不证明面身份或姿态到位。

停止可以在长等待/拖动期间发布，不依赖心跳新鲜性、不等待普通命令队列：

```powershell
python tools/deep_dive_continuous_capture.py stop --session logs/deep_dive_capture/board-a-forward-001
```

也可在录制终端按Ctrl+C。停止先释放本工具持有的输入，再封闭回调接收、关闭采集、排空原图、卸载输入/前台观察、排空时间线。普通关闭不停止或杀死游戏，也不终止其他Python/Node进程。

## 参数与完整性门

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `--fps` | 30 | 请求的WGC更新上限附近速率，实际以回调时间为准 |
| `--duration` | 600秒 | ready后的录制时限，范围5–2400秒 |
| `--queue-frames` | 64 | 待编码原图上限，约数百MiB内存；不能靠无限缓存掩盖慢写盘 |
| `--max-gap` | 0.25秒 | 相邻实际回调超过此值，停止并标记不完整 |
| `--stall-sec` | 3秒 | 长时间没有新回调即停止；不把静止/断流自动判停稳 |
| `--min-free-gib` | 2GiB | 磁盘保留空间，低于此值停止 |

这些是**采集运行保护参数**，不是新识别算法阈值。首次实机不能因素材失败而不留记录地放宽；如需改变参数，应另开会话并保留原失败素材与参数。

以下任一情况使 `recording_complete=false`、进程返回非零：队列满/写盘失败、尺寸或窗口位置变化、无法映射客户区、回调间隙超限/断流、开始记录后更换native session、输入失败、observer失败、关闭/排空无法确认。

关闭有明确等待预算：原生关闭10秒、PNG排空30秒、observer关闭5秒、时间线排空10秒。超时保存incomplete报告及未确认项，不宣称原生资源全部释放；阻塞的清理线程为daemon，进程退出后由OS回收本进程资源。异常强杀/断电时可能只有`.part`和部分JSONL，没有summary；这样的素材不得当完整采集。

完整退出的summary还要具备：实际收到帧数>0且与保存帧数相等；无原图/日志队列丢项；writer已停止；采集关闭及observer卸钩已确认。`recording_complete=true`只表示采集完整，始终 `formal_scan_success=false`。

## 下一次实机安排

1. 先做短时间静止与一次小拖动的采集，确认图像颜色/客户区映射、实际回调间隔、原图保存与输入时间对齐；任何失败完整留存。
2. 然后开启新会话，从reset前开始连续记录；每个可读区域做接近、松手持续观察、越过及反向接近，保存所有面交接。
3. 至少再换一种盘面内容重复，记录盘面/方向/人工阶段标记；不让当前识别器决定阶段结束。
4. 使用原图和连续时间线作独立标注，之后才进入重设计文档中的离线原型阶段。

本轮没有执行以上步骤。当前实现的首次实机行为、平台hook兼容性和吞吐仍待该次授权采集确认。
