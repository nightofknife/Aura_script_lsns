param(
    [Parameter(ValueFromRemainingArguments = $true, Position = 0)]
    [string[]]$Args,
    [string]$VenvPython = ".venv\\Scripts\\python.exe"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $VenvPython)) {
    throw "Venv python not found: $VenvPython"
}

$env:PYTHONNOUSERSITE = "1"
& $VenvPython cli.py @Args
