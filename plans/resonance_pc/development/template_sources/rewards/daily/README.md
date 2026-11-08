# 每日任务奖励素材

正式运行目录为 `templates/rewards/daily/`，匹配目录为
`data/meta/daily_reward_templates_pc.json`，路径均相对 `plans/resonance_pc/`。
运行时只读取正式 PNG、遮罩及匹配参数，不依赖游戏资源、开发工具或本文目录。

本批正式素材逐文件复制自用户已审阅的候选，没有在任务实现期间重绘、重新校正或执行匹配。
`native_provenance.json` 保留 sprite、Prefab、字体与素材指纹；
`reward_refinements.json` 保存奖励标题和退出提示的原生重建配方。

入口使用完整的带红点图标，红点参与 RGB 匹配。入口和金／蓝礼物的遮罩仅保留
`alpha >= 224` 的区域。奖励标题及退出提示使用灰度字形和周边暗区，遮罩为用户认可的
“图标矩形＋文字矩形”；退出提示对模板与截图统一使用 `blur` 预处理。

`main_entry.png`（无红点）、`incomplete.png` 和 `go_to.png` 保留为对照素材，目录中标记
`diagnostic_only`；不能仅凭无红点模板未命中或带红点模板未命中，认定当前处于主界面。
任务是否跳过还必须由运行流程确认主界面。

`screenshots/` 是原始用户供图，`screenshots.json` 记录 SHA-256。
四张 1282×752 的流程截图及另一次无红点主界面图均保留原始窗口边框；
客户区固定裁剪 `[1, 31, 1281, 751]`（XYXY），不缩放。供图只用于校正与审阅，
没有将截图裁片作为原生模板像素。

`calibration_summary.json` 保留素材准备阶段的历史回放结果。规则矩形遮罩下，
“获得物品”为 0.971、退出提示为 0.921；各自其他供图页面的最高分为 0.265、0.394。
奖励弹层只有一张正例且参与了校正，分数不是独立准确率，也不表示实机流程已验证。
任务实现期间没有重跑这些回放。

需要重新制作时，从仓库根目录调用：

```powershell
python -B tools/build_daily_reward_templates.py `
  --game-data 'D:/path/to/Resonance/雷索纳斯_Data' `
  --devtools-src 'D:/path/to/aura-resonance-devtools/src' `
  --pythonlibs 'D:/path/to/isolated-unitypy-dependencies'
```

`--pythonlibs` 可省略，表示使用当前 Python 环境。依赖只用于素材制作，不加入运行包。
工具只读取本机原生资源并写入正式模板、来源报告及匹配目录；不截图、不匹配、不控制游戏。
游戏资源升级后生成的像素可能改变，须另行审阅，不能沿用本批历史分数作为新版本结论。
