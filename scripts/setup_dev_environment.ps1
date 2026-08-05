param(
    [string]$BasePython = "",
    [string]$VenvPath = ".venv",
    [ValidateSet("cuda", "cpu", "none")]
    [string]$VisionProvider = "cuda",
    [switch]$SkipMuMuAssets,
    [switch]$SkipPreflight
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runtimeSetup = Join-Path $PSScriptRoot "setup_python_runtime.ps1"
$preflight = Join-Path $PSScriptRoot "build_preflight.ps1"

Push-Location $repoRoot
try {
    $setupArgs = @{
        VenvPath = $VenvPath
        VisionProvider = $VisionProvider
        FetchMuMuAssets = -not $SkipMuMuAssets
    }
    if ($BasePython) {
        $setupArgs.BasePython = $BasePython
    }

    & $runtimeSetup @setupArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Runtime environment setup failed with exit code $LASTEXITCODE."
    }

    $venvPython = Join-Path $VenvPath "Scripts\python.exe"
    Write-Host "Installing pinned development and GUI dependencies ..."
    & $venvPython -m pip install -r requirements/dev.txt
    if ($LASTEXITCODE -ne 0) {
        throw "Development dependency installation failed with exit code $LASTEXITCODE."
    }

    & $venvPython -m pip check
    if ($LASTEXITCODE -ne 0) {
        throw "Development environment dependency check failed with exit code $LASTEXITCODE."
    }

    if (-not $SkipPreflight) {
        & $preflight -VenvPython $venvPython
        if ($LASTEXITCODE -ne 0) {
            throw "Development environment preflight failed with exit code $LASTEXITCODE."
        }
    }

    Write-Host ""
    Write-Host "Development environment ready: $venvPython"
    Write-Host "Run the GUI with: .\scripts\run_cli.ps1 gui resonance"
}
finally {
    Pop-Location
}
