# 深潜实体识别的隔离 DirectML 运行时

`execution_provider=dml_worker` 是显式可选后端。现有 CPU/auto 后端保持原行为。主进程不替换、不降级、不重新绑定其 ONNX Runtime；仅实体模型的私有子进程加载隔离目录内的 `onnxruntime-directml==1.24.4`。OCR 和其他主进程模型继续使用原环境。

## 部署到另一台机器

需要 Windows、支持 DirectX 12 的显卡，以及与应用 Python **相同版本和架构**的隔离环境。示例在仓库根执行，仅安装到新环境；不是修改现有 `.venv` 的指令：

```powershell
$cache = Join-Path (Get-Location) '.pytest_tmp/dml-runtime-install'
New-Item -ItemType Directory -Path $cache -Force | Out-Null
$env:TEMP = $cache
$env:TMP = $cache
$env:TMPDIR = $cache
$env:PIP_CACHE_DIR = Join-Path $cache 'pip-cache'
.venv/Scripts/python.exe -m venv .venv-dml
.venv-dml/Scripts/python.exe -m pip install -r requirements/optional-vision-onnx-directml-isolated.txt
```

普通源码运行默认用当前 `sys.executable` 启动 worker。worker 先从应用环境加载 NumPy/OpenCV，再仅在导入隔离 ORT 时短暂添加 `runtime_site`。打包程序必须显式指定可运行上述源码 worker、具备应用依赖的 Python；首版没有冻结 EXE 内嵌 worker 支持。不要配置一个没有 NumPy/OpenCV/Aura 依赖的独立 Python。

配置键位于 `resonance_pc.deep_dive.entity_detector` 下：

```yaml
execution_provider: dml_worker
dml_device_id: 1
session:
  intra_op_num_threads: 4
worker:
  runtime_site: .venv-dml/Lib/site-packages
  ort_version: 1.24.4
  # 源码运行通常不需要此项；冻结应用必须提供。
  # python_executable: .venv/Scripts/python.exe
  startup_timeout_sec: 15
  request_timeout_sec: 5
  close_timeout_sec: 5
```

相对路径以仓库根解析，也允许明确的绝对路径。`runtime_site` 必须显式提供，没有指向研究缓存的隐藏默认值。显卡序号按该机器 DXGI 枚举确定，不能照搬示例的 `1`；研究机器的序号 1 是 RTX 3060 Laptop。进程启动时检查隔离包分发名、实际导入路径和版本，以及实际 session 的主 provider。缺包、错版本、错设备初始化失败，均不会自动改用 CPU。若需 CPU，显式选择原 `cpu` 后端重新启动任务。

## 服务接口与证据

`EntityDmlWorkerBackend.initialize()` 返回实际子进程元数据。`ready` 和 `status` 是属性；服务状态的 `worker` 字段保留这些元数据。主要字段：`ort_version`、`ort_path`、`runtime_site`、`distribution`、`provider`、`providers`、`device_id`、`model_sha256`、`classes`、输入/输出形状。设备编号是实际传入成功 session 创建的编号；ORT 的 provider options 可能为空，不能从空字典推导显卡名称。

`run(rgb)` 返回 `(raw, letterbox, diagnostic)`；输入必须是 `uint8 [720,1280,3]` RGB。服务继续使用现有解码、NMS、弱框遮挡、玩家头局部融合和精确 RGB 缓存，不在 worker 重复实现分类和票据逻辑。诊断含当次 `request_id`、`rgb_sha256`、`model_sha256`、`raw_sha256`、实际 provider 与模型推理耗时。`ipc_total_sec` 包含快照、整帧传输、子进程预处理/推理、回传和主进程校验。

二进制协议使用 `DDW1` 魔数、协议版本、长度前缀 JSON 和二进制内容。JSON 上限 16 KiB，载荷上限 3 MB；RGB 长度固定为 2,764,800 字节。请求严格递增，回应须匹配当前请求、RGB 和模型 SHA256。原始输出必须为有限 FP32 `[1,7,N]`、`0<N<=20000`，且字节长度和哈希吻合。主进程再次调用同一 `prepare_rgb_roi` 验证 letterbox。实际模型还须通过原 `validate_model` 的输入/三类别元数据检查。

worker 没有队列缓存，也不传游戏输入。stdout 专供协议；Python 第三方日志重定向 stderr，stderr 直接写仓库 `.pytest_tmp/deep_dive_dml_workers/<id>/stderr.log`，避免无人读取的日志管道塞满。子进程 cwd 是仓库根，TEMP/TMP/TMPDIR 位于该日志目录。创建进程使用隐藏窗口。

初始化、推理、协议或身份失败直接抛错，不把空预测作为成功。坏 worker 不会在后续 `run()` 静默重载；应关闭服务后显式重建。`close()` 串行等待当前请求完成，再请求 drained ACK，确认子进程正常退出，返回 `{drained, error}`；超时只终止该实例拥有的子进程并返回明确失败。多个阶段各有有界等待，失败后的 terminate/kill 清理还各允许最多 2 秒。close 可重复调用；服务应该保留首次 close 失败诊断，不能把后续幂等调用当成原失败已成功 drained。

## 2026-10-02 离线验证边界

23 项真实子进程协议/生命周期回归通过，覆盖帧/模型/版本/路径身份错误、无穷/NaN、非法形状、输出摘要、超时、子进程死亡、stderr 大量日志和 drained close。测试使用假模型 worker，不需要 GPU。

正式生产服务 `dml_worker` 与当前主环境 ORT 1.27.0 CPU4 对比了 30 张真实保存 RGB：baseline90 会话 15 张、c4 会话 10 张、d458 会话 5 张。共 69 个实体的类别、数量、整数框、来源和 `confirmable` 全部一致；最大置信度差 `5.07e-7`，最大点坐标差 `5.34e-5 px`。主进程 ORT 对象和 1.27.0 路径未变，子进程实际为 1.24.4 DirectML。四张语义回放的校正理由及实体格候选也一致。

| 同一套服务、CV1 | CPU ORT 1.27.0 / intra4 | 隔离 DML 1.24.4 / device1 |
|---|---:|---:|
| 初始化 | 131 ms | 1688 ms |
| 单帧实体服务中位数 | 72.0 ms | 48.0 ms |
| 单帧实体服务 P95 | 80.6 ms | 53.8 ms |
| 4 张完整语义平均 | 130.3 ms | 107.5 ms |

上述 DirectML 数字包含 IPC、主进程重复预处理校验及原有融合。语义回放使用最终已知图案 atlas 和空历史，不能代表实时队列负载。证据原文件位于 `.pytest_tmp/deep_dive_goal_20261002/research/dml_worker_production_comparison.json`。这是成本和输出一致性检验，不是准确率真值检验；隔离 worker 的完整实机连续 20 次验收仍须主代理在同一冻结版本执行。
