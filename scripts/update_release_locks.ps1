param(
    [string]$BasePython = "",
    [switch]$RecreateEnvironment
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$toolVenv = Join-Path $repoRoot ".venv-release-lock-tools"
$contract = Get-Content -Raw -LiteralPath (Join-Path $repoRoot "packaging\release-contract.json") | ConvertFrom-Json

if (-not $BasePython) {
    $selector = "-$($contract.python.major_minor)"
    $BasePython = (& py $selector -c "import sys; print(sys.executable)").Trim()
    if ($LASTEXITCODE -ne 0) { throw "Python $($contract.python.major_minor) is required to generate release locks." }
}
if ($RecreateEnvironment -and (Test-Path -LiteralPath $toolVenv)) {
    Remove-Item -LiteralPath $toolVenv -Recurse -Force
}
if (-not (Test-Path -LiteralPath (Join-Path $toolVenv "Scripts\python.exe"))) {
    & $BasePython -m venv $toolVenv
    if ($LASTEXITCODE -ne 0) { throw "Failed to create the release lock environment." }
}

$python = Join-Path $toolVenv "Scripts\python.exe"
& $python -m pip install --disable-pip-version-check "pip-tools==7.5.2"
if ($LASTEXITCODE -ne 0) { throw "Failed to install pip-tools." }

$locks = @(
    @{ Input = "requirements\release-cpu.txt"; Output = "requirements\release-cpu.lock.txt" },
    @{ Input = "requirements\release-gpu.txt"; Output = "requirements\release-gpu.lock.txt" },
    @{ Input = "requirements\release-nvidia-overlay.txt"; Output = "requirements\release-overlay.lock.txt" }
)

Push-Location $repoRoot
try {
    foreach ($lock in $locks) {
        & $python -m piptools compile $lock.Input `
            --output-file $lock.Output `
            --generate-hashes `
            --allow-unsafe `
            --strip-extras
        if ($LASTEXITCODE -ne 0) { throw "Failed to generate $($lock.Output)." }
    }
}
finally {
    Pop-Location
}

Write-Host "Release locks regenerated for Windows and Python $($contract.python.major_minor)."
