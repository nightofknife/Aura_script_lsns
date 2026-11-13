# 通行证奖励素材

运行目录为 `templates/rewards/battle_pass/`，参数目录为
`data/meta/battle_pass_templates_pc.json`，路径相对 `plans/resonance_pc/`。
运行时无需访问游戏资源、开发工具、原始截图或临时分析目录。

原有 10 个模板直接复制自用户认可的候选。
保留完整带红点星形入口、两组页签状态、任务一键领取、带红点等级一键领取，
以及从每日奖励标题裁出的白色物品图标和复用的回主界面按钮。
任务奖励和等级奖励弹窗均只匹配该图标，不匹配标题文字、右下角加号或空白退出提示。
有任务可领取时直接识别任务一键领取，不识别单卡领取按钮。
左侧卡片提醒使用 `pass_reminder.png`，由原生 `Img_Red` 引用的 `main_icon_tip` 生成。
两页普通一键领取外观相同，不能单独作为当前页面的依据。

`native_provenance.json` 保存原生 bundle、Prefab、字体、配方和图像指纹；
`battle_pass_refinements.json` 保存用户确认后的精灵/字体布局配方；
`matching_profiles.json` 是重建工具写出目录时使用的匹配参数来源。
遮罩使用规整矩形，入口遮罩仅保留不透明核心区域。

带红点等级一键领取使用 `manual_claim_all_red.weights.npy` 浮点遮罩，文字权重为 1，
红点权重为 4。运行时用 `np.load(path, allow_pickle=False)` 读取 float32 数组后交给视觉服务，
不能把它转换成普通二值 PNG，否则历史区分度结果不再适用。
奖励图标为 46×45，遮罩只包含灰度不低于 224 的最大连通白色主体，不包含加号或背景。
使用灰度平方差匹配（阈值 0.9），按遮罩有效像素数归一化 `score_scale`。
对命中的同一位置再用相同遮罩确认笔画灰度变化（相关系数阈值 0.5），排除浅色面板和纯白块。

`screenshots/` 保存五张原始用户供图，`screenshots.json` 保存指纹，客户区裁剪为
`[1,31,1281,751]`（XYXY），不缩放。截图只作历史校准参考，不作为模板像素来源。
`calibration_summary.json` 保留接入前的历史匹配结果；同一批供图参与过校准，不能当作独立准确率。
自动化回归覆盖供图回放、去除文字/退出提示/加号、负样本及弹窗关闭流程；未做实机领取。

原生重建命令（从仓库根目录执行）：

```powershell
python -B tools/build_battle_pass_templates.py `
  --game-data 'D:/path/to/Resonance/雷索纳斯_Data' `
  --devtools-src 'D:/path/to/aura-resonance-devtools/src' `
  --pythonlibs 'D:/path/to/isolated-unitypy-dependencies'
```

`--pythonlibs` 可省略。工具复用每日模板工具的原生资源读取接口，但不调用每日生成入口，
也不修改每日资源。通行证 renderer 与配方已进入正式目录，不依赖 `.analysis_tmp`。
奖励图标从每日正式标题资源裁出，主页锚点直接复制每日正式资源。
生成命令不会截图、匹配或控制游戏。
原生资源或共享每日资源更新后需重新审阅，不能沿用旧校准结果。
