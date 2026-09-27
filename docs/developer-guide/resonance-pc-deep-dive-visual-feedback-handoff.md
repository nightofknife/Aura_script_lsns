# 识海深潜视觉反馈跨机器交接

日期：2026-09-27。分支：`codex/deep-dive-visual-feedback-plan`。
基线：`925b096c6ae0270ede4d6890a51128d17c99b236`（单局节点处理与循环流程，PR #89）。

原始交接提交 `a07de9bf` 只包含方案、实施计划和证据摘要，没有新增运行时代码、训练权重、素材或客户端反编译源码。后续已新增[实验扫描任务](resonance-pc-deep-dive-scan-test.md)，已在当前棋盘连续5次实机完成54/54格及全部目标，含报告生成耗时30.3–48.1秒；尚未跨布局验收，未合并主线。

后续需求与资料更新：用户提供了 `2026-09-27 16-39-22.mp4` 和玩家/灵感/奇点三张参考图，并确认三类目标互斥。输出扩展为目标位置与无目标格的节点图标；目标下方图标无需识别。新增[完整布局扫描设计与调研](resonance-pc-deep-dive-full-layout-design.md)，该文的需求范围优先于下方历史四分类摘要。新录像和参考图仍为本地资料，未随 Git 分发，历史权重缺失状态没有因此改变。

## 获取与开始

在另一台机器已有仓库中执行；切换前保留当地未提交工作：

```powershell
git fetch origin
git switch --track origin/codex/deep-dive-visual-feedback-plan
git status
```

若本地已经存在该分支，使用 `git switch codex/deep-dive-visual-feedback-plan`。尚未克隆时：

```powershell
git clone --branch codex/deep-dive-visual-feedback-plan https://github.com/nightofknife/Aura_script_lsns.git
cd Aura_script_lsns
```

阅读顺序：

1. [方案与游戏逻辑](resonance-pc-deep-dive-visual-feedback-plan.md)。
2. [分阶段实施和验收](resonance-pc-deep-dive-visual-feedback-implementation.md)。
3. 当前 AGENTS.md、现有单局/事件/循环代码和所用输入后端。
4. 执行 P0 资产核验，然后开展 P1 离线结构跟踪；无需先操作正在运行的游戏。

## 已完成、已研究与未完成

| 内容 | 交接时状态 |
|---|---|
| 简单随机移动、节点事件、单局/循环运行 | 已在本分支基线中，不是本次新增 |
| 固定四角度和灵敏度采集 | 已有历史任务/参数，可作数据基线 |
| 帧率影响拖动的原因、重置和相机模式语义 | 客户端静态分析已完成，指纹如下 |
| 四分类模型 | 历史离线试验已有结果；本次未交付权重或可复现训练包 |
| 连续输入、快慢视觉分离、六面局部坐标图 | 已实现，同棋盘5次完整扫描通过；跨布局/跨回合世界坐标尚未验收 |
| 完整感知接入后续规划 | 待 P1–P4 通过后实施 |

## 素材与模型可用性

2026-09-27 检查原开发机时，历史四分类目录 `.pytest_tmp/deep_dive_4class_20260926` 存在但没有可用文件。该目录未进入 Git，不能假设切换机器后存在。

用户此前提供过旧/新批次截图、人工清洗材料以及 2026-09-26 录制的魔方视频。它们不随本分支分发，新的开发环境需另行取得并登记路径、SHA256、来源、采集局、分辨率、帧率、标注版本和划分。文档不依赖原机器的盘符。

历史试验用于说明已有方向，不是本次重新验证的结果：

- 类别顺序：empty、player、boss、inspiration。
- 曾选用四通道 160×160 输入的迁移分类模型；RGB 与格子 mask 的预处理细节必须随原模型恢复，不能凭此摘要重建。
- 历史测试中 player 为 11/11、Boss 为 6/6 接受且正确；inspiration 15 个样本中 11 个接受且正确、3 个误判 empty、1 个拒绝。empty 454 个样本中有 2 个误判 inspiration。
- 样本小、部分测试反复用于诊断，不能代表独立新局或连续任意姿态泛化；新方案需要重新评估。
- 旧观察槽位 V1L/V1R/V2/V3L/V3R/V4 尚不能直接作为规范世界面编号使用。

历史交付记录（本次未找到文件，仅用于未来恢复时比对）：

| 项目 | 记录 |
|---|---|
| ONNX | deep_dive_cell_4class_transfer.onnx，6,102,159 字节 |
| ONNX SHA256 | 59fd6464dac033e7ac7e996ba03695aa623c2bc5e666247c184b94398772cedd |
| ZIP 大小 | 7,669,609 字节 |
| ZIP SHA256 | 4cfee6765d2eb1bcc5a933d3799fbe77c246679f2a29f6761854268a766bfb77 |

恢复不到原模型/预处理/标注时，应重训可复现基线，不能用同名三分类模型替代。结构跟踪和控制回放可先行，不依赖四分类权重。不要默认提交用户视频、大批截图或游戏资源到 Git；先设计资产交付与版本清单。

## 客户端证据指纹

下面是原开发机只读检查保存的 SHA256。前三项当前字节码转换后与已有分析缓存一致；DragHandle 与此前分析指纹一致。指纹用于确认分析所针对的版本，不证明另一台机器相同，更不证明实机闭环已通过。

| 文件 | SHA256 |
|---|---|
| UICubeRogueMainDataModel.lua | d8fe14d7d1c494b3184755e2b5e77baf79671c1f973181f188292ab0079b2c3c |
| UICubeRogueMainViewFunction.lua | 08270352da9ac1bef9ffa8b6b933c4e43386fab367df49a555fc0cdadbcd3c27 |
| UICubeRoguewMainController.lua | 817d031a8c864d28b66bdc766d7833f6abfa4abbbc6cd75dd02c0eee818d1917 |
| DragHandle.lua | 53a543bb5553571a6cf53e76b7247f3e339c64c40bf63c017d2c2edcde283176 |

前三个模块来自客户端 `雷索纳斯_Data/Patch/Script/UICubeRogueMain`。在实际安装目录定位 DragHandle，使用 PowerShell `Get-FileHash -Algorithm SHA256 -LiteralPath <实际文件路径>` 核验；尖括号表示待替换值。若指纹不同，重新检查 DragHandle 输入运算、RotateToFaceUp、SetCameraToMove、指针转发与真正的游戏层旋转入口。

不随分支提供客户端完整反编译内容。方案文档保存了必要的行为摘要、函数名、关键条件与反编译歧义，足够定位复核。更复杂的反编译输出有控制流警告，不能直接作为可执行源码或精确几何标定。

## 接续时首先回答的问题

- 截图后端能否区分输入前后帧，时间戳到底表示采集还是接收？
- 哪些结构特征在动态特效下仍可靠，如何拒绝对称面身份歧义？
- 重置参考如何与当前玩家面及规范世界坐标关联？
- 实际预制模型、相机变化和 Boss 浮空偏移如何校准？
- 哪个输入后端用于 P3，最短有效拖动和安全短段范围是多少？
- 六面覆盖和唯一目标定位需要哪些独立证据才能交给规划器？

这些都是待验证项。先交付可回放的观测与控制诊断，再增加自动执行范围。

