# 识海深潜实体模型

`deep_dive_entities.onnx` 为方案自带的三类实体检测模型，供
`resonance_pc_deep_dive_entity_detector` 默认加载。模型文件随 Git 跟踪；
其他研究模型继续被忽略。原始研究权重保留在本机原路径，此副本没有重新训练或转换。

类别顺序为 `player_head`、`singularity`、`inspiration`。输入为静态 FP32
`[1,3,640,640]` RGB，输出为不含 NMS 的 `[1,7,8400]`。服务负责固定 ROI、
letterbox、归一化、类别内 NMS 与轻量融合；相关说明见
[实体服务文档](../../../../docs/developer-guide/deep-dive-entity-detector-service.md)。

文件为 10,566,854 字节，SHA-256：

```
bff7444233525475ef25173845a6c197c1eec993f2544d204a44846260718123
```

在仓库根目录可验证：

```powershell
Get-FileHash -LiteralPath plans/resonance_pc/data/models/deep_dive_entities.onnx -Algorithm SHA256
```

机器仍需安装服务文档列出的 ONNX Runtime 依赖。完整模型契约和校验值见
`deep_dive_entities.json`。此模型已进行保存帧验证；模型随包并不表示已经达到
99% 的整个扫描流程准确率。
