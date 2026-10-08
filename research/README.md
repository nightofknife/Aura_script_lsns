# 识海深潜研究入口

当前继续开发的方案是固定四视角、视觉反馈竖直旋转和稳定裁格读取。
接入设计见 `docs/developer-guide/resonance-pc-deep-dive-four-view-integration-20261006.md`；
实机证据见 `docs/developer-guide/resonance-pc-deep-dive-four-view-live-20261006.md`。

## 当前模块

- `deep_dive_four_view_template_alignment.py`：每面独立配准；目前依赖当前板的模板，不等于跨棋盘完成。
- `deep_dive_four_view_pose_feedback.py`：主面/双侧面的有符号图像残差。
- `deep_dive_four_view_node_reader.py`：固定归一化裁格读取。
- `deep_dive_four_view_reading_check.py`：离线真值评分及三帧融合原型，不能作为正式成功门。
- `tools/deep_dive_four_view_servo_experiment.py`：实机研究入口，默认5轮，位置参数 `1` 演示一轮。
- `tools/deep_dive_vertical_capture_broker.py` 与 `deep_dive_vertical_command.py`：研究输入协议。

## 历史研究

旧线锚到位、逐面取景、匿名格、图案格、响应拟合等模块保留用于其已有回归和历史复现，
不作为当前方案的默认接入路径。源代码和相应测试应成组退出，不能单删被import模块。
`read_saved_front_view` 仍间接依赖旧面扫描模块；其探针配置默认使用 CPU。

`tools/deep_dive_live_acceptance.py` 是旧七路线、单轮不超过90秒的验收工具。
其报告只适用于该旧扫描协议，不能作为 `four_views` 方案的实机验收证明。

## 研究输入与运行方式

当前板标注和 calibration02 原图是研究材料，不随源码提交。离线评分和实机伺服入口
必须显式传入 `--annotations` JSON 以及 `--reference-root` 图片根目录。
JSON 中每个 `views` 元素包含 `view`、`face`、`image`、`cells`（以及评分使用的 `source`）；
`image` 相对图片根目录解析，需提供覆盖视角1至4的1280×720原图。
工具在创建检测器或发出任何游戏操作前检查所有参考图片，缺少输入会清楚报错。

从仓库根目录运行，例如以下离线评分命令；输出限制在本仓库 `.pytest_tmp` 内：

```powershell
.venv\Scripts\python.exe research/deep_dive_four_view_reading_check.py --annotations <标注JSON> --reference-root <图片根目录> --output .pytest_tmp/four_view_reading
```

`read_saved_front_view.py <保存帧目录> [U|R|F|D|L|B]` 和离线评分默认使用 CPU。
仅当同时显式提供 `--dml-runtime-site`、`--dml-python`、`--dml-device` 时，才使用隔离的
DirectML worker；`--dml-ort-version` 默认 `1.24.4`，应与所提供运行时一致。
不再引用某台机器的旧实验虚拟环境或默认选择设备1。

实机研究工具需要已启动的研究 capture broker 及其 `.pytest_tmp` 下的 `commands` 目录。
以下命令会实际操作游戏，应仅在用户明确授权后运行；`--help` 不发出操作：

```powershell
.venv\Scripts\python.exe tools/deep_dive_four_view_servo_experiment.py .pytest_tmp/<broker目录> 1 --annotations <标注JSON> --reference-root <图片根目录>
```

离线识别结果、历史实机报告和当前实现的实机验收应分别记录；导入研究工具或通过回归测试
不证明当前 `four_views` 已完成实机验收。

2026-10-06已删除旧实验约6GiB的连续冗余截图。旧报告JSON/日志和被选中姿态仍保留，
部分旧实验不再具备完整逐帧回放；原图删减清单在 `.pytest_tmp/deep_dive_cleanup_20261006/`。
验收5轮、用户观看演示、当前校准和原始训练素材未清除。
