param(
    [string]$VenvPython = ".venv\\Scripts\\python.exe",
    [string]$SpecPath = "packaging\\pyinstaller\\aura.spec",
    [string]$RuntimeRoot = ".runtime",
    [string]$ReleaseName = "aura-release",
    [string]$PyInstallerVersion = "6.14.2",
    [ValidateSet("cpu", "gpu")]
    [string]$OnnxRuntimeProfile = "gpu",
    [switch]$IncludeNvidia,
    [switch]$IncludeGui,
    [switch]$CreateZip,
    [switch]$CreateNvidiaOverlay,
    [switch]$SkipBuild,
    [switch]$SkipAssemble
)

$ErrorActionPreference = "Stop"

function Assert-PathExists {
    param([string]$PathValue, [string]$Label)
    if (-not (Test-Path $PathValue)) {
        throw "$Label not found: $PathValue"
    }
}

function Invoke-RobocopySafe {
    param(
        [string]$Source,
        [string]$Destination,
        [string[]]$ExtraArgs = @()
    )

    Assert-PathExists -PathValue $Source -Label "Robocopy source"
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null

    $args = @(
        $Source,
        $Destination,
        "/E",
        "/NFL",
        "/NDL",
        "/NJH",
        "/NJS",
        "/NP"
    ) + $ExtraArgs

    & robocopy @args | Out-Null
    $code = $LASTEXITCODE
    if ($code -ge 8) {
        throw "Robocopy failed with exit code $code while copying '$Source' -> '$Destination'."
    }
    $global:LASTEXITCODE = 0
}

function Test-PythonModuleAvailable {
    param(
        [string]$PythonPath,
        [string]$ModuleName
    )

    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $PythonPath -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('$ModuleName') else 1)" *> $null
        return $LASTEXITCODE -eq 0
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
}

function Assert-PythonModulesAbsent {
    param(
        [string]$PythonPath,
        [string[]]$ModuleNames
    )

    $presentModules = @()
    foreach ($moduleName in $ModuleNames) {
        if (Test-PythonModuleAvailable -PythonPath $PythonPath -ModuleName $moduleName) {
            $presentModules += $moduleName
        }
    }

    if ($presentModules.Count -gt 0) {
        throw (
            "Release venv is not clean. These build-only or excluded runtime modules are importable: " +
            ($presentModules -join ", ") +
            ". Use a clean release venv for the selected ONNX Runtime profile."
        )
    }
}

function Assert-PythonModulesPresent {
    param(
        [string]$PythonPath,
        [string[]]$ModuleNames
    )

    $missingModules = @()
    foreach ($moduleName in $ModuleNames) {
        if (-not (Test-PythonModuleAvailable -PythonPath $PythonPath -ModuleName $moduleName)) {
            $missingModules += $moduleName
        }
    }

    if ($missingModules.Count -gt 0) {
        throw (
            "Release venv is missing modules required by the selected packaging options: " +
            ($missingModules -join ", ") +
            ". Install the matching requirements before building."
        )
    }
}

function Assert-OnnxRuntimeEnvironment {
    param(
        [string]$PythonPath,
        [ValidateSet("cpu", "gpu")]
        [string]$Profile
    )

    $script = @'
from importlib import metadata
import sys

profile = sys.argv[1]

def installed_version(name):
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None

onnxruntime_version = installed_version("onnxruntime")
onnxruntime_gpu_version = installed_version("onnxruntime-gpu")

if profile == "cpu":
    if not onnxruntime_version:
        raise SystemExit("onnxruntime is required for the CPU ONNX release venv.")
    if onnxruntime_gpu_version:
        raise SystemExit("Do not install onnxruntime-gpu in the CPU release venv.")
    distribution = "onnxruntime"
elif profile == "gpu":
    if not onnxruntime_gpu_version:
        raise SystemExit("onnxruntime-gpu is required for the GPU ONNX release venv.")
    if onnxruntime_version:
        raise SystemExit("Do not install both onnxruntime and onnxruntime-gpu in the release venv.")
    distribution = "onnxruntime-gpu"
else:
    raise SystemExit(f"Unsupported ONNX Runtime release profile: {profile!r}")

try:
    import onnxruntime as ort
except ImportError as exc:
    raise SystemExit(f"Failed to import onnxruntime from {distribution}: {exc}") from exc

providers = list(ort.get_available_providers())
if "CPUExecutionProvider" not in providers:
    raise SystemExit(f"ONNX Runtime CPUExecutionProvider is missing: {providers!r}")

print(
    "ONNX Runtime release preflight OK: "
    f"profile={profile}; distribution={distribution}; providers=" + ",".join(providers)
)
'@

    $output = $script | & $PythonPath - $Profile 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "ONNX Runtime preflight failed for profile '$Profile': $output"
    }
    if ($output) {
        Write-Host $output
    }
}

function Assert-OcrModelBundle {
    param(
        [string]$ModelsRoot,
        [string]$PythonPath,
        [string]$ValidatorPath
    )

    $bundleDir = Join-Path $ModelsRoot "ppocrv5_server"
    Assert-PathExists -PathValue $bundleDir -Label "OCR ONNX model bundle"

    Assert-PathExists -PathValue $ValidatorPath -Label "OCR bundle validator"
    & $PythonPath $ValidatorPath $bundleDir
    if ($LASTEXITCODE -ne 0) {
        throw "OCR ONNX model bundle validation failed with exit code $LASTEXITCODE."
    }
}

function Get-FileProductVersion {
    param([string]$PathValue)

    if (-not (Test-Path $PathValue)) {
        return $null
    }

    $item = Get-Item -LiteralPath $PathValue
    $rawVersion = $item.VersionInfo.ProductVersion
    if (-not $rawVersion) {
        $rawVersion = $item.VersionInfo.FileVersion
    }
    if (-not $rawVersion) {
        return $null
    }

    $match = [regex]::Match($rawVersion, "\d+(\.\d+){1,3}")
    if (-not $match.Success) {
        return $null
    }

    try {
        return [version]$match.Value
    }
    catch {
        return $null
    }
}

function Update-MsvcRuntimeForOnnxRuntime {
    param([string]$RuntimeDir)

    $targetPath = Join-Path $RuntimeDir "_internal\msvcp140.dll"
    if (-not (Test-Path $targetPath)) {
        return
    }

    $sourcePath = Join-Path $env:SystemRoot "System32\msvcp140.dll"
    if (-not (Test-Path $sourcePath)) {
        Write-Warning "System MSVC runtime was not found at $sourcePath; packaged onnxruntime-gpu may require a newer VC runtime on target machines."
        return
    }

    $targetVersion = Get-FileProductVersion -PathValue $targetPath
    $sourceVersion = Get-FileProductVersion -PathValue $sourcePath
    if ($null -eq $sourceVersion) {
        Write-Warning "Could not determine System32 msvcp140.dll version; leaving packaged MSVC runtime unchanged."
        return
    }

    if ($null -eq $targetVersion -or $sourceVersion -gt $targetVersion) {
        Write-Host "Updating packaged msvcp140.dll for ONNX Runtime: $targetVersion -> $sourceVersion"
        Copy-Item -LiteralPath $sourcePath -Destination $targetPath -Force
    }
}

function Write-ReleaseConfig {
    param(
        [string]$TemplatePath,
        [string]$DestinationPath,
        [ValidateSet("cpu", "gpu")]
        [string]$Profile
    )

    Assert-PathExists -PathValue $TemplatePath -Label "Config template"

    $executionProvider = if ($Profile -eq "cpu") { "cpu" } else { "auto" }
    $foundExecutionProvider = $false
    $updatedLines = Get-Content -LiteralPath $TemplatePath |
        ForEach-Object {
            if ($_ -match "^\s*execution_provider\s*:") {
                $foundExecutionProvider = $true
                "  execution_provider: $executionProvider"
            }
            else {
                $_
            }
        }

    if (-not $foundExecutionProvider) {
        throw "Config template does not contain ocr.execution_provider; update $TemplatePath before packaging."
    }

    $updatedLines | Set-Content -Path $DestinationPath -Encoding UTF8
}

function Copy-OcrModels {
    param(
        [string]$Source,
        [string]$Destination,
        [string]$PythonPath,
        [string]$ValidatorPath
    )

    Assert-OcrModelBundle -ModelsRoot $Source -PythonPath $PythonPath -ValidatorPath $ValidatorPath
    Invoke-RobocopySafe `
        -Source $Source `
        -Destination $Destination `
        -ExtraArgs @("/XD", "__pycache__", ".pytest_cache", "/XF", "*.pyc", "*.pyo")
    Assert-OcrModelBundle -ModelsRoot $Destination -PythonPath $PythonPath -ValidatorPath $ValidatorPath
}

function Copy-YoloModels {
    param(
        [string]$Source,
        [string]$Destination
    )

    if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
        return
    }

    Invoke-RobocopySafe `
        -Source $Source `
        -Destination $Destination `
        -ExtraArgs @("/XD", "__pycache__", ".pytest_cache", "/XF", "*.pyc", "*.pyo")
}

function Copy-PlanPackages {
    param(
        [string]$RepoRootPath,
        [string]$Destination,
        [string]$PythonPath,
        [string]$PackagerPath
    )

    Assert-PathExists -PathValue (Join-Path $RepoRootPath "plans") -Label "Plans directory"
    Assert-PathExists -PathValue $PackagerPath -Label "Plan package builder"
    & $PythonPath $PackagerPath --repo-root $RepoRootPath --destination $Destination
    if ($LASTEXITCODE -ne 0) {
        throw "Filtered Plan tree assembly failed with exit code $LASTEXITCODE."
    }
}

function Ensure-PyInstaller {
    param(
        [string]$PythonPath,
        [string]$Version
    )

    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $PythonPath -c "import PyInstaller" *> $null
        $hasPyInstaller = $LASTEXITCODE -eq 0
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }

    if ($hasPyInstaller) {
        return
    }

    Write-Host "Installing PyInstaller $Version into build venv ..."
    & $PythonPath -m pip install "pyinstaller==$Version"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install PyInstaller $Version."
    }
}

function Build-GuiRootLauncher {
    param(
        [string]$PythonPath,
        [string]$LauncherSource,
        [string]$DistPath,
        [string]$WorkPath
    )

    Assert-PathExists -PathValue $LauncherSource -Label "GUI root launcher source"
    New-Item -ItemType Directory -Force -Path $DistPath | Out-Null
    New-Item -ItemType Directory -Force -Path $WorkPath | Out-Null

    Write-Host "Building AuraResonanceGui root launcher ..."
    & $PythonPath -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --uac-admin `
        --name AuraResonanceGui `
        --distpath $DistPath `
        --workpath $WorkPath `
        $LauncherSource

    if ($LASTEXITCODE -ne 0) {
        throw "AuraResonanceGui root launcher build failed with exit code $LASTEXITCODE."
    }
}

function Get-NvidiaPackageRoot {
    param([string]$PythonPath)

    $script = @'
from pathlib import Path
import site

for site_root in site.getsitepackages():
    candidate = Path(site_root) / "nvidia"
    if candidate.is_dir():
        print(candidate)
        break
'@
    $path = ($script | & $PythonPath - 2>$null | Select-Object -First 1)
    if (-not $path) {
        throw "NVIDIA Python runtime packages were not found in the release venv. Install the required nvidia-* CUDA/cuDNN wheels before using -CreateNvidiaOverlay."
    }
    return [string]$path
}

function Assert-NvidiaRuntimeOverlayBundle {
    param([string]$NvidiaRoot)

    Assert-PathExists -PathValue $NvidiaRoot -Label "NVIDIA Python runtime root"
    $requiredDlls = @(
        "cu13\bin\x86_64\cublas64_13.dll",
        "cu13\bin\x86_64\cublasLt64_13.dll",
        "cu13\bin\x86_64\cudart64_13.dll",
        "cu13\bin\x86_64\cufft64_12.dll"
    )

    $missingDlls = @()
    foreach ($relativePath in $requiredDlls) {
        $candidate = Join-Path $NvidiaRoot $relativePath
        if (-not (Test-Path $candidate)) {
            $missingDlls += $relativePath
        }
    }

    # CUDA component wheels use the shared cu13 directory, while the cuDNN
    # wheel keeps its DLLs in its own nvidia\cudnn tree on Windows.
    $cudnnDll = Get-ChildItem -LiteralPath $NvidiaRoot -Recurse -File -Filter "cudnn64_9.dll" |
        Select-Object -First 1
    if (-not $cudnnDll) {
        $missingDlls += "cudnn64_9.dll under the NVIDIA runtime root"
    }

    if ($missingDlls.Count -gt 0) {
        throw (
            "NVIDIA runtime overlay is incomplete. Missing: " +
            ($missingDlls -join ", ") +
            ". Install requirements\\release-nvidia-overlay.txt into the release venv before using -CreateNvidiaOverlay."
        )
    }
}

function New-ZipArchive {
    param(
        [string]$SourcePath,
        [string]$DestinationPath
    )

    Assert-PathExists -PathValue $SourcePath -Label "Zip source"
    if (Test-Path $DestinationPath) {
        Remove-Item -LiteralPath $DestinationPath -Force
    }

    Write-Host "Creating zip archive: $DestinationPath"
    Compress-Archive -Path $SourcePath -DestinationPath $DestinationPath -Force
}

function Update-ReleaseChecksums {
    param([string]$ReleaseDirectory)

    if (-not (Test-Path $ReleaseDirectory)) {
        return
    }

    $zipFiles = @(Get-ChildItem -LiteralPath $ReleaseDirectory -File -Filter "*.zip" | Sort-Object Name)
    if ($zipFiles.Count -lt 1) {
        return
    }

    $checksumPath = Join-Path $ReleaseDirectory "SHA256SUMS.txt"
    $lines = foreach ($zipFile in $zipFiles) {
        $hash = (Get-FileHash -LiteralPath $zipFile.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        "$hash  $($zipFile.Name)"
    }
    $lines | Set-Content -Path $checksumPath -Encoding ascii
    Write-Host "Updated release checksums: $checksumPath"
}

function New-NvidiaRuntimeOverlay {
    param(
        [string]$PythonPath,
        [string]$RuntimeRootPath,
        [string]$ReleaseName
    )

    $nvidiaSource = Get-NvidiaPackageRoot -PythonPath $PythonPath
    Assert-NvidiaRuntimeOverlayBundle -NvidiaRoot $nvidiaSource

    $overlayRoot = Join-Path $RuntimeRootPath "release\\$ReleaseName-nvidia-overlay"
    $overlayReleaseRoot = Join-Path $overlayRoot $ReleaseName
    $overlayRuntimeInternal = Join-Path $overlayReleaseRoot "runtime\\_internal"
    $overlayNvidiaDir = Join-Path $overlayRuntimeInternal "nvidia"
    $overlayZip = Join-Path $RuntimeRootPath "release\\$ReleaseName-nvidia-overlay.zip"

    if (Test-Path $overlayRoot) {
        Remove-Item -LiteralPath $overlayRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $overlayRuntimeInternal | Out-Null

    Write-Host "Assembling NVIDIA runtime overlay ..."
    Invoke-RobocopySafe `
        -Source $nvidiaSource `
        -Destination $overlayNvidiaDir `
        -ExtraArgs @("/XD", "__pycache__", ".pytest_cache", "/XF", "*.pyc", "*.pyo")

    @(
        "NVIDIA runtime overlay for $ReleaseName"
        ""
        "Extract this archive into the same parent directory as $ReleaseName.zip."
        "It contains the same top-level folder name as the main GPU package:"
        "$ReleaseName\\runtime\\_internal\\nvidia"
        ""
        "The main package intentionally keeps these CUDA/cuDNN runtime libraries external."
    ) | Set-Content -Path (Join-Path $overlayReleaseRoot "NVIDIA-RUNTIME-OVERLAY.txt") -Encoding UTF8

    if (Test-Path $overlayZip) {
        Remove-Item -LiteralPath $overlayZip -Force
    }
    Write-Host "Creating NVIDIA runtime overlay zip: $overlayZip"
    Compress-Archive -Path $overlayReleaseRoot -DestinationPath $overlayZip -Force
}

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$VenvPythonPath = Join-Path $RepoRoot $VenvPython
$SpecFilePath = Join-Path $RepoRoot $SpecPath
$RuntimeRootPath = Join-Path $RepoRoot $RuntimeRoot
$DistPath = Join-Path $RuntimeRootPath "dist"
$WorkPath = Join-Path $RuntimeRootPath "build\\pyinstaller"
$LauncherDistPath = Join-Path $RuntimeRootPath "launcher-dist"
$LauncherWorkPath = Join-Path $RuntimeRootPath "build\\launcher"
$ReleaseRoot = Join-Path $RuntimeRootPath "release\\$ReleaseName"
$BuiltRuntimeDir = Join-Path $DistPath "aura"
$BuiltGuiRuntimeExe = Join-Path $BuiltRuntimeDir "AuraResonanceRuntime.exe"
$BuiltGuiLauncherExe = Join-Path $LauncherDistPath "AuraResonanceGui.exe"
$ReleaseRuntimeDir = Join-Path $ReleaseRoot "runtime"
$ReleasePlansDir = Join-Path $ReleaseRoot "plans"
$ReleaseOcrModelsDir = Join-Path $ReleaseRoot "models\\ocr"
$ReleaseYoloModelsDir = Join-Path $ReleaseRoot "models\\yolo"
$GuiLauncherSource = Join-Path $RepoRoot "packaging\\launcher\\aura_resonance_launcher.py"
$RunTemplate = Join-Path $RepoRoot "packaging\\templates\\run.ps1"
$ConfigTemplate = Join-Path $RepoRoot "packaging\\templates\\config.yaml"
$SourcePlansDir = Join-Path $RepoRoot "plans"
$SourceOcrModelsDir = Join-Path $RepoRoot "models\\ocr"
$SourceYoloModelsDir = Join-Path $RepoRoot "models\\yolo"
$PlanPackager = Join-Path $RepoRoot "scripts\\release\\build_plan_package.py"
$OcrBundleValidator = Join-Path $RepoRoot "scripts\\release\\validate_ocr_bundle.py"
$SourceLicense = Join-Path $RepoRoot "LICENSE"
$SourceReadme = Join-Path $RepoRoot "README.md"

if ($OnnxRuntimeProfile -eq "cpu") {
    if ($IncludeNvidia) {
        throw "-IncludeNvidia is only valid with -OnnxRuntimeProfile gpu."
    }
    if ($CreateNvidiaOverlay) {
        throw "-CreateNvidiaOverlay is only valid with -OnnxRuntimeProfile gpu."
    }
}

Assert-PathExists -PathValue $VenvPythonPath -Label "Venv python"
Assert-PathExists -PathValue $SpecFilePath -Label "PyInstaller spec"
Assert-PathExists -PathValue $RunTemplate -Label "Run script template"
Assert-PathExists -PathValue $ConfigTemplate -Label "Config template"
Assert-PathExists -PathValue $SourcePlansDir -Label "Plans directory"
Assert-PathExists -PathValue $PlanPackager -Label "Plan package builder"
Assert-PathExists -PathValue $OcrBundleValidator -Label "OCR bundle validator"

$env:PYTHONNOUSERSITE = "1"
$env:AURA_PKG_INCLUDE_NVIDIA = if ($IncludeNvidia) { "1" } else { "0" }
$env:AURA_PKG_INCLUDE_GUI = if ($IncludeGui) { "1" } else { "0" }

if (-not $SkipBuild) {
    Ensure-PyInstaller -PythonPath $VenvPythonPath -Version $PyInstallerVersion
}

$isOverlayOnly = $CreateNvidiaOverlay -and $SkipBuild -and $SkipAssemble
if ((-not $SkipBuild) -or $CreateNvidiaOverlay) {
    $forbiddenModules = @(
        "paddle",
        "paddleocr",
        "paddlex",
        "torch",
        "torchvision",
        "ultralytics"
    )
    if ((-not $IncludeGui) -and (-not $isOverlayOnly)) {
        $forbiddenModules += @("PySide6", "shiboken6")
    }

    Assert-PythonModulesAbsent -PythonPath $VenvPythonPath -ModuleNames $forbiddenModules
    if ($IncludeGui -and (-not $SkipBuild)) {
        Assert-PythonModulesPresent -PythonPath $VenvPythonPath -ModuleNames @("PySide6", "shiboken6")
    }
    Assert-OnnxRuntimeEnvironment -PythonPath $VenvPythonPath -Profile $OnnxRuntimeProfile
}

if (-not $SkipBuild) {
    Write-Host "Building Aura runtime with PyInstaller ..."
    New-Item -ItemType Directory -Force -Path $DistPath | Out-Null
    New-Item -ItemType Directory -Force -Path $WorkPath | Out-Null

    & $VenvPythonPath -m PyInstaller `
        --noconfirm `
        --clean `
        --distpath $DistPath `
        --workpath $WorkPath `
        $SpecFilePath

    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller build failed with exit code $LASTEXITCODE."
    }

    if ($IncludeGui) {
        Assert-PathExists -PathValue $BuiltGuiRuntimeExe -Label "Built Resonance GUI runtime executable"
        Build-GuiRootLauncher `
            -PythonPath $VenvPythonPath `
            -LauncherSource $GuiLauncherSource `
            -DistPath $LauncherDistPath `
            -WorkPath $LauncherWorkPath
        Assert-PathExists -PathValue $BuiltGuiLauncherExe -Label "Built AuraResonanceGui root launcher executable"
    }
}

if (-not $SkipAssemble) {
    Assert-PathExists -PathValue $BuiltRuntimeDir -Label "Built runtime directory"
    if ($IncludeGui) {
        Assert-PathExists -PathValue $BuiltGuiRuntimeExe -Label "Built Resonance GUI runtime executable"
        Assert-PathExists -PathValue $BuiltGuiLauncherExe -Label "Built AuraResonanceGui root launcher executable"
    }
    Update-MsvcRuntimeForOnnxRuntime -RuntimeDir $BuiltRuntimeDir

    if (Test-Path $ReleaseRoot) {
        Remove-Item -LiteralPath $ReleaseRoot -Recurse -Force
    }

    New-Item -ItemType Directory -Force -Path $ReleaseRoot | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $ReleaseRoot "logs") | Out-Null

    Write-Host "Assembling release root ..."
    Invoke-RobocopySafe -Source $BuiltRuntimeDir -Destination $ReleaseRuntimeDir
    if ($IncludeGui) {
        Copy-Item -LiteralPath $BuiltGuiLauncherExe -Destination (Join-Path $ReleaseRoot "AuraResonanceGui.exe") -Force
    }
    Update-MsvcRuntimeForOnnxRuntime -RuntimeDir $ReleaseRuntimeDir
    Copy-PlanPackages `
        -RepoRootPath $RepoRoot `
        -Destination $ReleasePlansDir `
        -PythonPath $VenvPythonPath `
        -PackagerPath $PlanPackager
    Copy-OcrModels `
        -Source $SourceOcrModelsDir `
        -Destination $ReleaseOcrModelsDir `
        -PythonPath $VenvPythonPath `
        -ValidatorPath $OcrBundleValidator
    Copy-YoloModels -Source $SourceYoloModelsDir -Destination $ReleaseYoloModelsDir

    Copy-Item -LiteralPath $RunTemplate -Destination (Join-Path $ReleaseRoot "run.ps1") -Force

    $releaseConfigPath = Join-Path $ReleaseRoot "config.yaml"
    Write-ReleaseConfig -TemplatePath $ConfigTemplate -DestinationPath $releaseConfigPath -Profile $OnnxRuntimeProfile

    if (Test-Path $SourceLicense) {
        Copy-Item -LiteralPath $SourceLicense -Destination (Join-Path $ReleaseRoot "LICENSE") -Force
    }
    if (Test-Path $SourceReadme) {
        Copy-Item -LiteralPath $SourceReadme -Destination (Join-Path $ReleaseRoot "README.md") -Force
    }

    $nvidiaRuntimeMode = if ($OnnxRuntimeProfile -eq "cpu") {
        "none"
    } elseif ($IncludeNvidia) {
        "bundled"
    } elseif ($CreateNvidiaOverlay) {
        "overlay"
    } else {
        "external"
    }
    $onnxRuntimeDistribution = if ($OnnxRuntimeProfile -eq "cpu") { "onnxruntime" } else { "onnxruntime-gpu" }
    $gpuBackend = if ($OnnxRuntimeProfile -eq "cpu") { "none" } else { "onnxruntime-gpu" }

    $summaryPath = Join-Path $ReleaseRoot "BUILD-INFO.txt"
    $summaryLines = @(
        "release_name=$ReleaseName"
        "built_at_utc=$([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ'))"
        "release_profile=$OnnxRuntimeProfile"
        "onnxruntime_distribution=$onnxRuntimeDistribution"
        "include_nvidia=$($IncludeNvidia.IsPresent)"
        "create_zip=$($CreateZip.IsPresent)"
        "create_nvidia_overlay=$($CreateNvidiaOverlay.IsPresent)"
        "ocr_backend=onnxruntime"
        "gpu_backend=$gpuBackend"
        "paddle_stack=false"
        "nvidia_runtime=$nvidiaRuntimeMode"
        "base_path_mode=release_root"
        "entrypoint=run.ps1"
    )
    if ($IncludeGui) {
        $summaryLines += @(
            "gui=true"
            "gui_entrypoint=AuraResonanceGui.exe"
            "gui_runtime=runtime\\AuraResonanceRuntime.exe"
        )
    }
    $summaryLines | Set-Content -Path $summaryPath -Encoding UTF8

    Write-Host "Release assembled at: $ReleaseRoot"
}

if ($CreateZip) {
    Update-MsvcRuntimeForOnnxRuntime -RuntimeDir $ReleaseRuntimeDir
    New-ZipArchive `
        -SourcePath $ReleaseRoot `
        -DestinationPath (Join-Path $RuntimeRootPath "release\\$ReleaseName.zip")
}

if ($CreateNvidiaOverlay) {
    New-NvidiaRuntimeOverlay `
        -PythonPath $VenvPythonPath `
        -RuntimeRootPath $RuntimeRootPath `
        -ReleaseName $ReleaseName
}

if ($CreateZip -or $CreateNvidiaOverlay) {
    Update-ReleaseChecksums -ReleaseDirectory (Join-Path $RuntimeRootPath "release")
}
