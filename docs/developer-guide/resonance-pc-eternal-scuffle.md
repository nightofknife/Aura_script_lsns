# PC 无垠乱斗

公开任务：`tasks:eternal_scuffle_pc.yaml:eternal_scuffle_pc`。GUI 入口为“小任务 → 活动玩法 → 无垠乱斗”，复用子进程运行器、全局任务互斥、取消和超时设置。

| 输入 | 规则 | 默认值 |
|---|---|---|
| `coins_per_run` | 整数 1～5 | 1 |
| `run_count` | 整数 1～9999 | 1 |

必须从无垠乱斗活动首页启动，真实客户区必须为 1280×720。任务不从城市导航、不自动续接中断的局。GUI 保存输入，只显示阶段、局数、每局结果、耗时和错误诊断位置。

## 编排与完成契约

任务 YAML 负责多局、五组选人配装、关卡推进、战利品与共用结算；独立 Action 执行具体操作。循环采用真正逐次等待的 `while`。三个 `scuffle_checked_*` 包装任务在每次 `aura.run_task` 返回后立即检查 `framework_data.nodes.finish.output`，要求 `success: true`、`status: completed`，缺少结果也阻止下一次循环。

1. 首页进入投币：1～4 枚执行一次最少加 N−1 次 `+1`，5 枚执行一次最多。间隔至少 0.4 秒，不识别币数或余额，不重跑投币序列。
2. 交替选择一名角色、一件开局装备，共五组。页面切换确认后才提交记录，之后核对队伍并确认默认队长。
3. 关卡页点击 START，只等待自动战斗结果。胜利下一步进入实际战利品或结算页面；失败下一步返回关卡页后，放弃并确认。
4. 两种结果共用动态开箱。全部箱子打开且返回按钮稳定出现后，返回并确认首页，再提交一次计数。

不读取关卡数字、不以固定关数结束。识别失败、异常、超时和取消均不会自动放弃、再次投币或增加完成局数。关卡循环另有有限迭代保护，命中保护不能当作完成。

会话使用 `core/state_store` 中的 `eternal_scuffle:<顶层 CID>` 键，承接子任务上下文深拷贝后的角色和装备变化；正常结束仅删除本次键。素材和观察器缓存使用 `PlanContext.cache`，玩家与库存持久缓存不参与本任务。

## 识别素材与策略

目录：`plans/resonance_pc/data/meta/eternal_scuffle.json`；图片：`templates/eternal_scuffle/`。包括 99 名角色、176 件装备、44 个控件，共 2966 张 PNG。装备包含可达专用条目 `11800086` 乱斗变量模块，其他测试装备不在候选库。

- **角色**：完整居中姓名模板主识别，保留空白上下文；持续不明确时使用游戏 Spine 生成的八相位人物辅助。
- **装备候选**：全部 176 个完整名称模板共同竞争，不调用 OCR，不以装备大图兜底。普通模板为 138×23；最长的 `“极坐标”指挥无人机` 为 164×23。原字体 23 号、比例 0.66，固定字号、单行溢出。两种宽度分别按卡片中心采样，再合并排名，避免短名匹配混入卡框边缘。
- **装备槽**：开局小槽使用 `iconPath`，分配小槽使用 `tipsPath`。空槽由空槽标识确认，非空槽由品质边框及可确认的图案核对，不能把未识别到图案当成空槽。
- **角色选中**：只匹配中央 SELECT 文字和绿底，不匹配动态四角。D07 蒙版排除外围 3 像素人物背景。轻量探测只扫描五个 SELECT 标志和唯一选中卡的姓名；零个、多个或姓名不确定均拒绝。

角色品质顺序为 SSR、SR、R、N，装备为 UR、SSR、SR、R。同品质先按 ID 降序，未知日期条目保留位置；已知实装日期仅在其余位置按日期降序、同日 ID 降序重排，运行时查整数排名。

战利品优先补空槽，其次改善对应槽中最低排名的旧装备，再以当前画面左上角色兜底。分配页重新识别位置；确认选中对象和完整队伍记录后才点右侧确认，切换页面后才更新装备。

## 等待、线程与取消

| 项目 | 参数 |
|---|---|
| 普通轮询 | 0.3 秒 |
| 页面确认／身份确认 | 连续 2 次／3 次一致 |
| 普通页面／转换和身份／战斗 | 20 秒／45 秒／600 秒 |
| 战斗轮询 | 1 秒 |
| 点击初始等待／补点观察窗 | 0.3 秒／2 秒 |
| 普通点击上限 | 首次加最多 3 次补点 |

控件和 SELECT 门槛 0.90；完整名称 0.86、首二名差 0.05；装备槽图案 0.88、差 0.10；人物辅助 0.93、差 0.05；宝箱 0.90。运行中不降低门槛。

点击后的两秒窗口只确认目标 SELECT 和姓名，不重复识别十五个装备槽。完整队伍核对使用独立的 45 秒预算；右侧确认的每次点击前再次检查选中 ID 和位置。原控件消失后停止补点，无效截图不能作为消失证据，候选变化不能继续点击旧 SELECT。

同步输入、窗口读取、素材加载、日志读写和 PNG 编码通过 `_run_blocking` 在线程执行；视觉探测复用共享 `poll_until` 的线程路径。取消会禁止尚未开始的排队操作，并等待已开始的操作收尾后再报告停止，避免后台输入或缓存读写残留。超时后返回的识别结果不算成功；这不承诺强制终止已经开始的本机调用。

## 事件与诊断

事件 `task.resonance_pc_eternal_scuffle_progress`，schema `resonance_pc.eternal_scuffle_progress.v1`。桥接按当前任务、CID、版本及递增 sequence 过滤，不从文本日志推断进度。

每次运行保存到 `logs/eternal_scuffle/<CID>/`：`events.jsonl`、正常完成的 `summary.json`，以及失败时的 `failure.json`、`last_frame.png`、`last_target.png`。候选分数、决策、点击次数和队伍变更写入事件，正常轮询不逐帧保存图片。

## 开发验证

正常运行仅依赖随包资源。`tools/build_eternal_scuffle_assets.py` 是离线构建工具，需要明确提供研究素材、原字体和原图来源，不作为生产启动步骤；每个正式资源有哈希，名称还保留字体来源哈希。

所有命令从当前仓库根运行。使用独立 pytest 临时目录，不清理 `.pytest_tmp` 根，避免删除研究源文件。

```powershell
$taskTemp = Join-Path (Get-Location) '.pytest_tmp/scuffle_env'
New-Item -ItemType Directory -Force -Path $taskTemp | Out-Null
$env:TEMP=$taskTemp
$env:TMP=$taskTemp
$env:TMPDIR=$taskTemp
python -m packages.aura_core.cli.package_cli sync plans/resonance_pc
python -m packages.aura_core.cli.package_cli check plans/resonance_pc
python -m packages.aura_core.cli.package_cli validate plans/resonance_pc
python tools/plan_doctor.py --plan resonance_pc
python -m pytest tests/unit/test_eternal_scuffle_policy.py tests/unit/test_eternal_scuffle_vision.py tests/unit/test_eternal_scuffle_runtime.py tests/unit/test_eternal_scuffle_async_boundaries.py tests/unit/test_eternal_scuffle_gui.py tests/unit/test_small_tasks_page.py tests/smoke/test_eternal_scuffle_replay.py --basetemp .pytest_tmp/scuffle_tests -q
```

针对性组合包含 167 项测试，覆盖素材完整性、相似姓名与长名、SELECT 与动态角框反例、线程响应与取消、GUI，以及真实 YAML／子进程回放。回放包含两局通关和失败放弃、7／4 箱、角色重排、缺少业务结果与取消阻断后续输入。

2026-09-06 实机分别验证了首页至首战，以及修复后从待分配装备页继续到通关返回：续测新增七场胜利、完成七次战后分配、打开七个宝箱，只计一次完成，未追加投币或补点。SELECT 当前画面三次只读确认耗时约 1.66 秒。该次是中断后续测，不等同于新代码从首页不中断跑完的新局；失败放弃和连续两局仍只有回放覆盖。
