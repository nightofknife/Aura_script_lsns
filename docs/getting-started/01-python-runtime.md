# Python 运行环境与入口

Aura Game Framework 当前默认使用 Python 3.12 虚拟环境。

## 1. 初始化运行环境

在项目根目录执行：

```powershell
.\scripts\setup_python_runtime.ps1
```

关键行为：

- 自动解析本机 Python `3.12.x`
- 创建或复用 `.venv`
- 安装 `requirements/runtime.txt`
- 根据 `-VisionProvider` 安装 ONNX Runtime vision 依赖，默认 `cuda`
- 如果存在 `requirements/runtime.lock` 则优先使用 lock
- 运行 `pip check`

## 2. 启动前校验

推荐在迁移、切换依赖或移动目录后执行：

```powershell
.\scripts\build_preflight.ps1
```

当前会检查：

- `.venv` 必须是 Python 3.12
- `include-system-site-packages = false`
- `PYTHONNOUSERSITE=1` 生效
- 如果存在 `requirements/runtime.lock`，已安装包必须与 lock 一致
- `pip check` 通过
- `cli.py --help` 可运行
- `packages.aura_game.EmbeddedGameRunner` 可以加载并识别 `aura_benchmark`

## 3. CLI 入口

### 查看游戏模块

```powershell
.venv\Scripts\python.exe cli.py games --all
```

### 查看任务

```powershell
.venv\Scripts\python.exe cli.py tasks aura_benchmark
```

### 运行任务

```powershell
.venv\Scripts\python.exe cli.py run aura_benchmark tasks:single_sleep.yaml --inputs "{\"duration_ms\": 50, \"scenario\": \"demo\"}"
```

在 PowerShell 下，推荐把输入写入 JSON 文件后配合 `--inputs-file` 使用，避免命令行引号转义问题。

### TUI

```powershell
.venv\Scripts\python.exe cli.py tui
```

说明：

- TUI 入口使用 `tui_manual` profile
- 适合人工执行 entry task 或调试调度项
- 不依赖 HTTP 服务

## 4. Python SDK

### EmbeddedRunner

```python
from packages.aura_game import EmbeddedGameRunner

runner = EmbeddedGameRunner()
runner.start()
print(runner.list_games())
runner.close()
```

### SubprocessRunner

```python
from packages.aura_game import SubprocessGameRunner

if __name__ == "__main__":
    with SubprocessGameRunner() as runner:
        print(runner.list_games())
```

说明：

- `EmbeddedGameRunner`
  适合脚本、测试、内部工具直接调用。
- `SubprocessGameRunner`
  适合宿主 GUI 或桌面程序把执行逻辑隔离到独立子进程。

## 5. 依赖文件

- `requirements/runtime.txt`
  运行时依赖。
- `requirements/dev.txt`
  开发与测试依赖。
- `requirements/optional-yolo-cpu.txt`
  YOLO 兼容入口，转向共享 ONNX Runtime CPU 推理依赖。
- `requirements/optional-yolo-cuda.txt`
  YOLO 兼容入口，转向共享 ONNX Runtime CUDA 12 推理依赖。
- `requirements/optional-vision-onnx-cpu.txt`
  OCR/YOLO 共享的 ONNX Runtime CPU 推理依赖。
- `requirements/optional-vision-onnx-cuda.txt`
  OCR/YOLO 共享的 ONNX Runtime CUDA 12 推理依赖。
- `requirements/optional-ocr-export.txt`
  OCR Paddle inference 模型导出为 ONNX 部署包的导出机依赖。

## 6. YOLO 部署流

运行时不再直接加载 `.pt`。本机只负责加载训练机/导出机交付的部署产物：

1. 交付并加载：
   - `model.onnx`
   - `model.meta.json`
2. 将两个文件放入 `models/yolo/`，或在任务配置中引用显式模型路径。

CPU 环境安装 `requirements/optional-vision-onnx-cpu.txt`，CUDA 环境安装 `requirements/optional-vision-onnx-cuda.txt`。`requirements/optional-yolo-cpu.txt` 和 `requirements/optional-yolo-cuda.txt` 仍保留为兼容入口。

完整说明见：

- [YOLO ONNX Runtime 部署与使用指南](../project-reference/yolo-onnx-runtime.md)

## 7. GPU 栈

当前仓库默认统一到 CUDA 12 + ONNX Runtime 路线：

- OCR 和 YOLO 运行时共用 `onnxruntime-gpu` 的 `CUDAExecutionProvider`
- Paddle/PaddleOCR/Paddle2ONNX 只属于 OCR 导出机，不再属于部署机运行时
- CPU 部署只安装 `onnxruntime`，CUDA 部署只安装 `onnxruntime-gpu`

如果需要 GPU 能力，建议避免在同一个环境里同时安装 `onnxruntime` 和 `onnxruntime-gpu`，否则 Windows 下可能出现 CUDA provider 不可见或回退到 CPU 的情况。

可以使用以下命令快速检查当前环境里的 GPU 栈、YOLO 运行时和 OCR 运行时：

```powershell
.venv\Scripts\python.exe tools\gpu_runtime_diagnostics.py --probe-ocr --onnx-model .runtime\smoke_yolo\smoke_yolo11n.onnx
```

## CUDA12 说明

- 对 OCR 和 YOLO，运行时只安装一个 ONNX Runtime 包：CPU 用 `requirements/optional-vision-onnx-cpu.txt`，CUDA 用 `requirements/optional-vision-onnx-cuda.txt`。
- `requirements/optional-ocr-export.txt` 只用于 OCR 导出工具链，不要把导出依赖当成部署机运行时依赖。
- 如果 GPU 探测结果不符合预期，重新运行：

```powershell
.venv\Scripts\python.exe tools\gpu_runtime_diagnostics.py --probe-ocr --onnx-model .runtime\smoke_yolo\smoke_yolo11n.onnx --json
```

`scripts/setup_python_runtime.ps1` 不再安装 Paddle/PaddleOCR 作为运行时依赖。OCR 和 YOLO 现在共享 ONNX Runtime。CUDA12 部署使用 `-VisionProvider cuda`，CPU-only 部署使用 `-VisionProvider cpu`，只安装基础框架、不安装视觉推理依赖时使用 `-VisionProvider none`。
