# 深潜原生投影：换机开发交接

2026-10-22，基线 `d39bc0a2`。这是暂停时的开发检查点，**识别问题尚未修复，四视角实机验收未通过**。生产扫描、拖动和任务注册均未接入此模块；`development/` 不随玩家发布包分发。

## 已保存内容

- `model.py`：固定原生相机、54 槽位、Icon/rim 投影与外壳深度代理。调用方显式提供数据路径；没有旧识别回退。
- `evidence/20261022/assets/`：运行此模型所需的数值导出、来源 hash、相机候选和动画终点检查。没有完整客户端资源包或反编译源码。
- `evidence/20261022/projection/`：人工对应的四帧投影验证、标注、源图与误差结果。
- `evidence/20261022/single_frame/`：单帧自动姿态初验、人工检查点与结果。
- `evidence/20261022/four_views/`：实际四视角尝试的全部 5 张扫描帧、候选、投影、输入及失败记录。
- `evidence/20261022/scripts/*.py.txt`：当时实验脚本的原样快照，供检查失败实现。它们不是可调用的生产识别入口，也不是第二套兜底逻辑。里面的 `.pytest_tmp`、日志路径是历史位置。
- `evidence/20261022/manifest.json`：每个归档文件的 SHA256 与原仓库相对路径；历史报告引用未归档的完整审计材料时，以此清单为实际保存范围。

桌面旧深潜、完整游戏资源、旧备份分支和整套历史运行日志没有并入 main。新机器继续开发需要游戏安装及常规开发环境；本目录中的模型检查和失败帧分析不依赖旧电脑或桌面目录。

## 为什么中途退出

实际尝试完成 **0/4** 标准视角：重置后拖动 4 次，保存 5 帧，18.703 秒后触发 `native_pose_continuity_or_quality_rejected`。

失败帧选中的姿态相对前帧跳变 32.58°，偏离预期旋转轴 31.62°，超过 25°/8° 的保护门。保护正确地停止了输入，问题发生在它之前的姿态估计与候选选择：

1. 原型用单向方向边缘距离评分，并未在评分阶段应用真实几何遮挡；错误投影也能靠近图案笔画或侧壁。低分不等于正确格位，失败叠加可直接看到错位。
2. 扫描器先用 `best_score + 0.15` 裁掉候选，之后才检查时间连续性。失败帧只剩两个错误姿态；分数 5.4445、相对前帧 6.84° 的候选在连续性检查前就被淘汰。
3. 审计发现还存在多个物理上可行的候选，故不能仅选最近一个就宣告正确。要增加当前图像中的对应证据来区分，不能靠放宽保护继续拖动。

原始单帧定位结果与四视角扫描终态不同；所有截图均为开发数据，不能作为独立盘面验收集。四个目标角 29.6/119.6/209.6/299.6 尚未验证六面覆盖。

## 恢复开发入口

先读 [识别设计](../../../../docs/developer-guide/resonance-pc-deep-dive-native-recognition-design.md)，再看 `four_views/RESULTS.md`、第 3/4 帧的 `source.png`、`automatic-overlay.png` 和候选列表。

下一步尚未实现：在同一原生模型内，把上一可信帧的 Icon 平面像素与当前帧建立时间对应，检查双向跟踪和空间分布，用原生平面三维点拟合根旋转，联合评价图像证据、遮挡与物理连续性。证据不足或多解不消失则保留 unknown；不能把先验预测当本次观察。

固定要求：最终只保留原生投影这一套识别；不增加兜底识别器；先不调整拖动控制。先离线回放失败序列并补独立盘面，再实际执行完整四视角扫描。当前 `model.py` 只是几何层，不是已完成的自动识别器。

在仓库根使用已有 NumPy 环境可以直接加载：

```python
from pathlib import Path
import numpy as np
from plans.resonance_pc.development.native_projection.model import NativeModel

assets = Path('plans/resonance_pc/development/native_projection/evidence/20261022/assets')
model = NativeModel(assets, assets / 'camera_native_candidate.json', assets / 'shell_triangles.json')
pixels = model.project(model.cells[0]['rim'], np.eye(3))
depth = model.raster_depth(np.eye(3))
```

相机是原生代码/父链推导的候选，不是实时遥测；深度是 Empty 外壳与不透明 Icon 的双面代理，不复现 shader、人物、特效或 UI。`visible()` 只检查朝向，不能证明无遮挡。不要据此直接授权点击。

所有后续测试从仓库根运行，输出和 TEMP/TMP/TMPDIR 放到仓库 `.pytest_tmp/<scope>`；不要覆盖这份冻结证据。

换机后可从仓库根执行 `python plans/resonance_pc/development/native_projection/verify_checkpoint.py`，核验 69 份证据的 hash、54 槽位、60 个已记录投影点、深度代理、缺资源显式失败和发布目录排除。提交前该检查与 `pytest tests/unit/test_release_plan_assembly.py -q`（8 项）通过。这些只是交接完整性和离线回归检查，不代表四视角识别通过。
