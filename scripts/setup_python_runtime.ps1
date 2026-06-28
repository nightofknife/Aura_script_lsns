param(
    [string]$BasePython = "",
    [string]$VenvPath = ".venv",
    [string]$RuntimeRequirements = "requirements/runtime.txt",
    [string]$LockFile = "requirements/runtime.lock",
    [ValidateSet("cuda", "cpu", "none")]
    [string]$VisionProvider = "cuda",
    [string]$VisionCpuRequirements = "requirements/optional-vision-onnx-cpu.txt",
    [string]$VisionCudaRequirements = "requirements/optional-vision-onnx-cuda.txt",
    [switch]$UseLock = $true,
    [switch]$FetchMuMuAssets = $true
)

$ErrorActionPreference = "Stop"

function Invoke-CheckedCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [string[]]$ArgumentList = @(),

        [switch]$CaptureOutput
    )

    if ($CaptureOutput) {
        $output = & $FilePath @ArgumentList
    } else {
        & $FilePath @ArgumentList
        $output = $null
    }

    if ($LASTEXITCODE -ne 0) {
        $renderedArgs = ($ArgumentList | ForEach-Object {
            if ($_ -match '\s') {
                '"' + $_ + '"'
            } else {
                $_
            }
        }) -join ' '
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $renderedArgs"
    }

    return $output
}

function Assert-PathExists {
    param([string]$PathValue, [string]$Label)
    if (-not (Test-Path $PathValue)) {
        throw "$Label not found: $PathValue"
    }
}

function Resolve-BasePython {
    param([string]$PathValue)
    if ($PathValue) {
        return $PathValue
    }

    $resolved = Invoke-CheckedCommand -FilePath "py" -ArgumentList @("-3.12", "-c", "import sys; print(sys.executable)") -CaptureOutput
    if (-not $resolved) {
        throw "Unable to resolve Python 3.12 via py launcher."
    }
    return $resolved.Trim()
}

function Ensure-VisionRuntime {
    param(
        [string]$PythonExe,
        [string]$Provider,
        [string]$CpuRequirements,
        [string]$CudaRequirements
    )

    if ($Provider -eq "none") {
        Write-Host "Skipping optional ONNX vision runtime installation."
        return
    }

    $requirements = $CpuRequirements
    if ($Provider -eq "cuda") {
        $requirements = $CudaRequirements
    }
    Assert-PathExists -PathValue $requirements -Label "Vision runtime requirements"
    Write-Host "Installing ONNX vision runtime ($Provider) from $requirements ..."
    Invoke-CheckedCommand -FilePath $PythonExe -ArgumentList @("-m", "pip", "install", "-r", $requirements)
}

$BasePython = Resolve-BasePython -PathValue $BasePython

Assert-PathExists -PathValue $BasePython -Label "Base Python"
Assert-PathExists -PathValue $RuntimeRequirements -Label "Runtime requirements"

$version = Invoke-CheckedCommand -FilePath $BasePython -ArgumentList @(
    "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"
) -CaptureOutput
if (-not $version.StartsWith("3.12.")) {
    throw "Base Python must be 3.12.x. Current: $version"
}

if (-not (Test-Path $VenvPath)) {
    Write-Host "Creating virtual environment at $VenvPath using $BasePython ..."
    Invoke-CheckedCommand -FilePath $BasePython -ArgumentList @("-m", "venv", "--copies", $VenvPath)
}

$venvPython = Join-Path $VenvPath "Scripts/python.exe"
Assert-PathExists -PathValue $venvPython -Label "Venv python"

$pyvenvCfg = Join-Path $VenvPath "pyvenv.cfg"
Assert-PathExists -PathValue $pyvenvCfg -Label "pyvenv.cfg"

$cfgText = Get-Content $pyvenvCfg -Raw
if ($cfgText -notmatch "include-system-site-packages\s*=\s*false") {
    $cfgText = [regex]::Replace(
        $cfgText,
        "include-system-site-packages\s*=\s*true",
        "include-system-site-packages = false"
    )
    Set-Content -Path $pyvenvCfg -Value $cfgText -Encoding UTF8
}

Write-Host "Installing runtime dependencies ..."
Invoke-CheckedCommand -FilePath $venvPython -ArgumentList @("-m", "pip", "install", "--upgrade", "pip", "wheel", "setuptools<82")

$refreshLock = $false
if ($UseLock -and (Test-Path $LockFile)) {
    Invoke-CheckedCommand -FilePath $venvPython -ArgumentList @("-m", "pip", "install", "-r", $LockFile)
} else {
    Invoke-CheckedCommand -FilePath $venvPython -ArgumentList @("-m", "pip", "install", "-r", $RuntimeRequirements)
    $refreshLock = $true
}

Ensure-VisionRuntime `
    -PythonExe $venvPython `
    -Provider $VisionProvider `
    -CpuRequirements $VisionCpuRequirements `
    -CudaRequirements $VisionCudaRequirements

if ($refreshLock) {
    Invoke-CheckedCommand -FilePath $venvPython -ArgumentList @("-m", "pip", "freeze", "--all") -CaptureOutput | Set-Content -Path $LockFile -Encoding UTF8
}

if ($FetchMuMuAssets) {
    $fetchScript = "scripts/fetch_mumu_runtime_assets.py"
    Assert-PathExists -PathValue $fetchScript -Label "MuMu asset fetch script"
    Write-Host "Fetching MuMu runtime assets ..."
    Invoke-CheckedCommand -FilePath $venvPython -ArgumentList @($fetchScript)
}

Write-Host "Running pip check ..."
Invoke-CheckedCommand -FilePath $venvPython -ArgumentList @("-m", "pip", "check")

$venvVersion = Invoke-CheckedCommand -FilePath $venvPython -ArgumentList @("-c", "import sys; print(sys.version)") -CaptureOutput
Write-Host ""
Write-Host "Runtime ready."
Write-Host "Base python : $BasePython"
Write-Host "Venv python : $venvPython"
Write-Host "Version     : $venvVersion"
