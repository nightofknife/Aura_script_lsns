# 公共观测与工坊普通模板迁移验证

日期：2026-10-22。范围是首批 `OperationScope`、公共观测服务、已有 wait action、运行时取消保护，以及工坊房间导航的普通模板适配器。

所有命令从仓库根目录执行，使用 PowerShell 7 和 `.venv/Scripts/python.exe -B`。TEMP、TMP、TMPDIR、日志、pytest cache 与 basetemp 均在 `.pytest_tmp/observation-batch1-validation-20261022/`。没有启动游戏、发送真实输入或加载 OCR 模型；识别差分使用录制图片和合成图片，时间与取消使用 fake clock、事件屏障及真实受追踪线程。

## 识别差分

以另一个模板优化工作认可的 ROI-only 旧实现为基线，SHA256 为 `867d18c8c0032e6a37abc8301836d699b1a46c1b520d737f27b5f05f799f3a1d`。本批没有修改该共享基线、独立收菜实现或 VisionService 内核。

共 **1086 组**，覆盖 **17 张录制图片、80 个普通模板**，**0 项非预期差异**。命中、位置、中心、列表顺序、页面判定及异常类型/消息逐项比较；置信度绝对容差为 `1e-6`，阈值另比较实际分数及相邻可表示值。源码、图片、模板在执行前后重算 SHA，没有漂移。

| 类别 | 组数 |
| --- | ---: |
| 录制图片 | 731 |
| 普通资产合成正负样本与扰动 | 320 |
| 异常输入/资源 | 18 |
| ROI | 9 |
| 阈值边界 | 5 |
| 人为注入非有限分数 | 3 |

有 **3 项明确的契约差异**，均为注入 NaN/Inf 的匹配分数图：旧内核逐项替换为 -1，新适配器对框架无法证明有效的结果抛出 `ValueError("Room template observation failed: invalid match result")`。其中 `[0.99, +Inf]` 旧实现可取有限命中，另外两项旧实现返回无命中。三项均固定旧值和新异常断言，未作为普通兼容通过隐藏；有效 uint8 图片中没有出现这类分数图。未为此添加私有匹配回退。

## 发现并修复的问题

1. **工坊资源加载异常兼容**：缺失文件、损坏图片、目录三种输入的异常类型或消息发生变化。适配器正常加载继续使用注入服务，仅在其合并为 FileNotFoundError 的失败边界还原旧读取异常；修复后差分通过。
2. **总超时预算**：三个模板 wait 入口在路径解析完成后才创建 scope，导致解析耗时不计入 timeout。现从解析前创建并复用同一 scope；fake clock 证明预算耗尽后不会再截图或识别。`timeout=0` 一次探测和外层预算继承继续覆盖。
3. **取消 drain 收尾竞态**：用户回调结束即可发出 completed，而受追踪 worker 的外层 trace 尚未结束。现正常路径等待结果交付和 trace 收尾，再释放租约、同步计数并宣告完成；确定性事件屏障回归覆盖该窗口。内部 dispatcher 被直接取消时不会据此宣告底层 worker 停止，继续按实际退出回收并保守执行有界 drain。

## 时间、取消与失败语义覆盖

- 父子预算只能缩短，取消优先于超时；迟到截图、OCR、模板结果和接受判断不能成功。
- 排队中、执行中及重复取消；正常 drain、零宽限隔离、超时后实际 worker 退出再恢复目标。
- 运行时与输入桥拒绝隔离目标的新操作；释放按键/鼠标仍作用于已有会话，不触发 HELLO 或重绑定。
- 有效无命中为 absent；截图、OCR、模板失败为 unknown。unknown 不能证明消失，空模板集明确失败。
- 稳定计数要求不同帧、相同语义与允许的中心移动；unknown 清空稳定计数。
- 同帧探针共享、探针失败隔离、不可变帧、RGB/BGR、客户区 ROI 单次偏移及 OCR 参数转发。

## 测试记录与复跑

分组完成：core 45 项、wait/OCR 152 项、导航与适配器 110 项、已有生命周期/注入相关 42 项、目标保护集成 3 项。它们与最终合并回归重叠，不相加作为总数。

首轮合并回归为 840 passed / 4 failed；4 项分别使用了已移除的 `nav.vision` 私有 hook 或仅 app 注入的旧断言。测试按显式 matcher/VisionService 注入更新，继续保留地图覆盖、安全点击、拖动后观测和无 OCR 依赖等业务断言。此前 wait 的 7 项私有 hook 及 1 项 OCR 注入参数序位断言也作了同类适配；这些与上面的真实生产缺陷分别记录。

最终合并回归：**30 个测试文件，844 passed，0 failed，18.19 秒**。执行前后 10 个关键源码及全部 30 个测试文件 SHA 无漂移。测试耗时仅用于记录执行，不代表性能提升。

复跑命令（仓库根，PowerShell 7）：`& ./.pytest_tmp/observation-batch1-validation-20261022/final/rerun.ps1`。该脚本设置 scope 内环境变量、读取文件列表、运行完整合并集并检查源码/测试漂移。

- [最终测试 XML](../../.pytest_tmp/observation-batch1-validation-20261022/final/combined/results.xml)
- [最终完整输出](../../.pytest_tmp/observation-batch1-validation-20261022/final/combined/pytest.log)
- [合并回归文件列表](../../.pytest_tmp/observation-batch1-validation-20261022/final/test-files.json)
- [复跑脚本](../../.pytest_tmp/observation-batch1-validation-20261022/final/rerun.ps1)
- [完整差分](../../.pytest_tmp/observation-batch1-validation-20261022/workshop/report.json)
- [可重跑差分脚本](../../.pytest_tmp/observation-batch1-validation-20261022/workshop/differential.py)
- [core 详细记录](../../.pytest_tmp/observation-batch1-validation-20261022/core/results.md)
- [wait 详细记录](../../.pytest_tmp/observation-batch1-validation-20261022/wait/results.md)
- [工坊详细记录](../../.pytest_tmp/observation-batch1-validation-20261022/workshop/results.md)

差分脚本每次使用新的 `--phase` 名，不覆盖原证据。pytest 文件列表与最终命令保存在最终执行目录，可按其列表复跑；必须显式指定 scope 内 basetemp，以免仓库默认 basetemp 清理其他对话证据。

上述结论限于覆盖的离线契约。实机动画、原生地图惯性、窗口身份及真实 OCR 耗时仍需独立实机验收；本次没有进行打包或性能基准。
