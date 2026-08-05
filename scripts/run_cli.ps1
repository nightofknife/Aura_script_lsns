param(
    [Parameter(ValueFromRemainingArguments = $true, Position = 0)]
    [string[]]$Args,
    [string]$VenvPython = ".venv\\Scripts\\python.exe"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $VenvPython)) {
    throw "Development environment not found: $VenvPython. Run .\scripts\setup_dev_environment.ps1 first."
}

$env:PYTHONNOUSERSITE = "1"
& $VenvPython cli.py @Args
