param(
    [string]$VenvPython = ".venv\\Scripts\\python.exe",
    [string]$LockFile = "requirements/runtime.lock"
)

$ErrorActionPreference = "Stop"

function Invoke-CheckedCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [string[]]$ArgumentList = @(),

        [switch]$CaptureOutput,

        [string]$StdinText = $null
    )

    if ($null -ne $StdinText) {
        if ($CaptureOutput) {
            $output = $StdinText | & $FilePath @ArgumentList
        } else {
            $StdinText | & $FilePath @ArgumentList
            $output = $null
        }
    } elseif ($CaptureOutput) {
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

function Normalize-LockLines {
    param([string[]]$Lines)
    return $Lines `
        | ForEach-Object { $_.Trim() } `
        | Where-Object { $_ -and -not $_.StartsWith("#") } `
        | ForEach-Object { $_.ToLowerInvariant() } `
        | Sort-Object -Unique
}

Assert-PathExists -PathValue $VenvPython -Label "Venv python"

$venvVersion = Invoke-CheckedCommand -FilePath $VenvPython -ArgumentList @(
    "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"
) -CaptureOutput
if (-not $venvVersion.StartsWith("3.12.")) {
    throw "Venv must be Python 3.12.x. Current: $venvVersion"
}

$pyvenvCfg = Join-Path (Split-Path (Split-Path $VenvPython -Parent) -Parent) "pyvenv.cfg"
Assert-PathExists -PathValue $pyvenvCfg -Label "pyvenv.cfg"
$cfgText = Get-Content $pyvenvCfg -Raw
if ($cfgText -notmatch "include-system-site-packages\s*=\s*false") {
    throw "Venv isolation check failed: include-system-site-packages must be false."
}

$env:PYTHONNOUSERSITE = "1"
$userSiteEnabled = Invoke-CheckedCommand -FilePath $VenvPython -ArgumentList @(
    "-c", "import site; print('1' if site.ENABLE_USER_SITE else '0')"
) -CaptureOutput
if ($userSiteEnabled -ne "0") {
    throw "User site packages are enabled. Expected disabled."
}

Write-Host "Validating lock consistency ..."
if (Test-Path $LockFile) {
    $freezeLines = Invoke-CheckedCommand -FilePath $VenvPython -ArgumentList @("-m", "pip", "freeze", "--all") -CaptureOutput
    $lockLines = Get-Content $LockFile

    $freezeNorm = Normalize-LockLines -Lines $freezeLines
    $lockNorm = Normalize-LockLines -Lines $lockLines

    $diff = Compare-Object -ReferenceObject $lockNorm -DifferenceObject $freezeNorm `
        | Where-Object { $_.SideIndicator -eq "<=" }
    if ($diff) {
        $preview = $diff | Select-Object -First 20 | Out-String
        throw "Installed packages are missing entries required by the lock file.`n$preview"
    }
} else {
    Write-Host "Lock file not found, skipping freeze diff."
}

Write-Host "Checking MuMu runtime assets ..."
Invoke-CheckedCommand -FilePath $VenvPython -ArgumentList @("scripts/fetch_mumu_runtime_assets.py", "--check")

Write-Host "Running pip check ..."
Invoke-CheckedCommand -FilePath $VenvPython -ArgumentList @("-m", "pip", "check")

Write-Host "Running startup smoke checks ..."
Invoke-CheckedCommand -FilePath $VenvPython -ArgumentList @("cli.py", "--help")
Invoke-CheckedCommand -FilePath $VenvPython -ArgumentList @("cli.py", "games", "--all")
Invoke-CheckedCommand -FilePath $VenvPython -ArgumentList @("cli.py", "tasks", "aura_benchmark")
Invoke-CheckedCommand -FilePath $VenvPython -ArgumentList @("cli.py", "games", "--runner", "subprocess", "--all")

Invoke-CheckedCommand -FilePath $VenvPython -ArgumentList @("-") -StdinText @'
from packages.aura_game import EmbeddedGameRunner

embedded = EmbeddedGameRunner()
games = embedded.list_games()
assert any(row["game_name"] == "aura_benchmark" for row in games)
embedded.close()
'@

Write-Host "Preflight passed."
