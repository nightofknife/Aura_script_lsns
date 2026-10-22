# 公共交互与转场迁移验证

日期：2026-10-22。范围是公共 `InteractionService`、查找点击 action、黑月准备/退出、工坊输入回执和确认，以及房间导航；同时回归其共享取消检查的访问/提交调用方。

所有命令从仓库根目录运行，使用 PowerShell 7 与 `.venv/Scripts/python.exe -B`。TEMP、TMP、TMPDIR、日志、cache、pytest basetemp 和所有差分产物均放在 `.pytest_tmp/interaction-batch2-validation-20261022/`。测试使用假输入后端、录制图片、合成像素、fake clock 与真实受追踪 worker/event；没有启动游戏、发送真实输入或加载 OCR 模型，也没有运行性能基准或打包。

## 新旧差分

旧实现取自本批实施前的 `.analysis_tmp/interaction-batch2-20261022/` 源码快照，没有恢复旧轮询作为生产回退。新旧两侧分别执行实际入口和公共服务，并记录输入、观察、返回及错误。

| 对照层 | 场景数 | 兼容维度一致 | 明确变化 | 未解释差异 |
| --- | ---: | ---: | ---: | ---: |
| 查找点击 action | 11 | 8 | 3 | 0 |
| 通用转场探针/补点 | 150 | 108 | 42 | 0 |
| 工坊 checked-click helper | 23 | 8 | 15 | 0 |
| 合计 | **184** | **124** | **60** | **0** |

这些数字是展开后的场景数，不是缺陷数量。60 项明确变化包含同类策略在不同补点上限/完成时刻的重复组合，不能表述为全部结果逐字节相同。转场对照比较旧的五个探针业务字段，新增目标身份元数据单独由回归测试覆盖；CancelledError 比较原生类型和输入阶段，不要求诊断消息保持旧空字符串。

### 查找点击

模板/文字命中和未命中、ROI 单次偏移、稳定后最终坐标、鼠标移动/点击顺序、按钮/时长、归一化多候选 OCR 及全部识别选项一致。None acknowledgement 继续保持未知。稳定匹配从三次重复解析同模板变为一次解析，是实现层变化，不额外计为差分场景。

三项明确收紧：迟到识别命中不再授权输入；明确 False acknowledgement 抛出 `input_rejected`；点击后等待也计入总预算，已发送一次点击后超时返回 False，不代表业务回滚。

### 转场

150 个场景覆盖目标优先、未知重置、源身份变化、帧对等价、首步授权、识别/比较迟到、最多 1–4 次点击、重新定位、源不明、取消和异常。

42 个明确变化分为三类、每类 14 个组合：

- 明确拒绝不再被当作成功输入，抛出 `InteractionError(code="input_rejected")`，只有一次尝试且回执 acknowledgement=False。
- 输入技术异常继续失败，并由 `InteractionError(code="input_failed")` 携带原原因链和回执；旧实现直接抛底层 OSError。
- 输入后立即取消时仍传播 CancelledError，并保留已发送阶段；新实现直接停止，不执行旧实现多余的 settle sleep。点击数和后续探针数保持相同，均无补点。

### 工坊

23 个场景逐项记录完整 record、领域 reason/data、异常类型/消息/原因链、输入与截图顺序、效果/就绪调用和虚拟耗时。15 项变化覆盖真实截止拒绝到期帧、耗时截图消耗预算、公共诊断原因链、取消回执、复用 record 安全修正、同帧消费、目标变化及父预算继承。每个变化都有命名场景和精确谓词，其他变化或源码漂移均使脚本非零退出。

源消失/unknown 后永久禁止补点的两条路径均覆盖：公共 `TransitionState` 逐个原始探针锁存；补人领域原有分支在源重现时仍只等目标。目标已到达时优先停止；显式允许补点的普通导航则必须重新确认和定位源。消费提交仍仅一次。

## 实测缺陷与修复

1. **最终验证回调可重绑定窗口**：原实现只在输入后发现 epoch 改变，回调已执行。现验证后、登记及发送前再次核对目标。
2. **无目标身份帧可授权已绑定窗口**：现拒绝 unverified Frame，移动和点击均不发生。
3. **默认 CID 取消未锁存**：自定义 async sleep/probe 取消时，CID registry 忽略 `"-"`，同 scope 仍可操作。新增公开 `OperationScope.cancel()` 设置共享 stop token 并登记 CID；没有修改 drain 算法。
4. **非法新坐标保留旧 record 成功标志**：现先清除输入/确认标志，再验证坐标；拒绝或非法的新操作不能被旧 `call_returned=True` 授权。
5. **黑月首步无效坐标错误改变**：首步源允许单帧时，公共层提前截获坐标错误，丢失原领域 code。现在生成源证据前完成领域坐标校验，保留 `black_moon_prepare_click_missing`。
6. **黑月点击拒绝返回值丢失**：领域点击回调没有返回 app.click 的值，公共回执看不到 False。现保留 bool/对象 success 返回；拒绝后立即失败并保留一次输入回执，不继续补点。

失败证据分别保存在 service 的 `initial.txt/xml`、workshop 的 `reused-record-initial.log/xml`、root 的 `adapted.log/xml` 和 `rejection-before.log/xml`。修复没有涉及共享匹配器、收菜 matcher、VisionService、OCR 内核或任务 YAML。

## 超时、取消与输入事实

- 迟到观察、等价比较和移动中取消不得授权后续点击；父子 scope 共用停止状态，总预算只能缩短。
- 发送前登记 attempted 快照；超时/取消在 worker 未返回时，可见 attempted=True、sent=False、call_returned=False、acknowledged=None。这表示结果未知，不能解释为没有发生输入或已经回滚。
- worker 已返回后保留 sent/acknowledged 的实际值；`ClickResult.sent=True, code="timeout"` 仅记录历史发送事实。
- drain 宽限到期时目标隔离，真实 worker 退出后再恢复。事件屏障覆盖 caller 取消、重复取消和迟到回执，没有放宽原生命周期断言。
- 同一源 Frame 不能再次授权输入；捕获后或最终验证中目标变化均在新输入前拒绝。
- probe 资源/识别异常继续传播；unknown 不证明目标达到，明确 False 不当作 None；异步入口拒绝未 await 的输入回调协程。

## 测试适配与覆盖边界

初轮旧测试中的私有 find/poll hook 改为真实 InteractionService/ObservationService 注入；业务坐标、输入顺序、required 消息、安全点击、消费次数及补人锁存断言保留。无操作的 sleep fake 改为显式推进的虚拟 scope 时钟。

黑月旧测试的 10–12 毫秒局部预算原本用于纯内联轮询；真实 worker 调度也使用剩余预算作为 asyncio timeout，故按同比例放大 fake 时间参数，继续检验相同的观察次数和顺序。取消由旧领域错误改为原生 CancelledError，是已声明的公共契约；访问测试仍检查 durable pending、零付款点击和未完成状态。

合并初轮为 1208 passed / 2 failed：另一个黑月访问测试文件还保留旧预算和取消异常断言；仅适配这两处后，该文件 49 项通过。首次较小 root 组的 70 个失败不能作为产品缺陷数量，其中包含旧总 scope/元数据断言和极小预算；适配后剩下的坐标错误及新增拒绝回执用例才用于定位真实修复。

查找点击的 11 个差分测试依赖本地旧源码快照，本轮全部实际执行；干净 checkout 缺少快照时只明确跳过这些比较，19 项普通 action 契约继续执行。

## 证据与复跑

最终合并为 **41 个文件、1210 passed、0 failed、0 skipped，22.13 秒**。执行前后 12 个关键源码、41 个测试文件及一个测试时钟支持文件的 SHA 均无漂移。分组的 68、47、365 和 136 项结果与此合并集重叠，不相加作为总数。测试耗时是执行记录，不代表性能收益。

- [最终测试 XML](../../.pytest_tmp/interaction-batch2-validation-20261022/final/results.xml)
- [最终完整输出](../../.pytest_tmp/interaction-batch2-validation-20261022/final/pytest.log)
- [41 个测试文件清单](../../.pytest_tmp/interaction-batch2-validation-20261022/final/test-files.json)
- [合并复跑脚本](../../.pytest_tmp/interaction-batch2-validation-20261022/final/run.ps1)
- [转场完整差分](../../.pytest_tmp/interaction-batch2-validation-20261022/root/classified-final-transition-report.json)
- [转场可重跑脚本](../../.pytest_tmp/interaction-batch2-validation-20261022/root/transition_differential.py)
- [工坊完整差分](../../.pytest_tmp/interaction-batch2-validation-20261022/workshop/final-differential.json)
- [公共服务详细记录](../../.pytest_tmp/interaction-batch2-validation-20261022/service/results.md)
- [action 详细记录](../../.pytest_tmp/interaction-batch2-validation-20261022/actions/results.md)
- [工坊详细记录](../../.pytest_tmp/interaction-batch2-validation-20261022/workshop/results.md)

合并复跑命令（仓库根，PowerShell 7）：`& ./.pytest_tmp/interaction-batch2-validation-20261022/final/run.ps1`。差分脚本每轮使用新的 `--phase`，不覆盖已有证据。所有测试 worker 均在 finally 中释放，`asyncio.run` 收尾其 executor；没有遗留浏览器或独立后台进程。

结论限于覆盖的离线行为和异常契约。实机动画、窗口切换、游戏接受输入及业务最终生效仍需独立实机验收。
