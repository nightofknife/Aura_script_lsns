param(
    [Parameter(ValueFromRemainingArguments = $true, Position = 0)]
    [string[]]$Args
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimeExe = Join-Path $root "runtime\\aura.exe"

if (-not (Test-Path $runtimeExe)) {
    throw "Runtime executable not found: $runtimeExe"
}

Push-Location $root
try {
    $env:AURA_BASE_PATH = $root
    $env:PYTHONNOUSERSITE = "1"
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"
    & $runtimeExe @Args
}
finally {
    Pop-Location
}
