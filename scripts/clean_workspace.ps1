param(
    [switch]$Apply,
    [switch]$PythonCachesOnly,
    [switch]$IncludeReleaseEnvironments,
    [switch]$IncludeCanonicalRuntime,
    [switch]$IncludePlanCaches
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

function Assert-SafeWorkspacePath {
    param([string]$PathValue)
    $resolved = [System.IO.Path]::GetFullPath($PathValue)
    $prefix = $repoRoot.TrimEnd('\') + '\'
    if (-not $resolved.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to operate outside the repository: $resolved"
    }
    return $resolved
}

function Get-DirectorySizeBytes {
    param([string]$PathValue)
    $measure = Get-ChildItem -LiteralPath $PathValue -File -Recurse -Force -ErrorAction SilentlyContinue |
        Measure-Object -Property Length -Sum
    return [int64]($measure.Sum)
}

$candidates = @()
foreach ($sourceRootName in @("packages", "plans", "scripts", "tests", "tools", "packaging")) {
    $sourceRoot = Join-Path $repoRoot $sourceRootName
    if (Test-Path -LiteralPath $sourceRoot) {
        $candidates += Get-ChildItem -LiteralPath $sourceRoot -Directory -Recurse -Force -Filter "__pycache__" -ErrorAction SilentlyContinue
    }
}
$rootPythonCache = Join-Path $repoRoot "__pycache__"
if (Test-Path -LiteralPath $rootPythonCache) {
    $candidates += Get-Item -LiteralPath $rootPythonCache -Force
}
if (-not $PythonCachesOnly) {
    $candidates += Get-ChildItem -LiteralPath $repoRoot -Directory -Force -Filter ".runtime-*" -ErrorAction SilentlyContinue
    foreach ($name in @(".pytest_cache", ".pytest_tmp", ".codex_tmp", ".playwright-cli", ".test-tmp", "build", "dist")) {
        $path = Join-Path $repoRoot $name
        if (Test-Path -LiteralPath $path) {
            $candidates += Get-Item -LiteralPath $path -Force
        }
    }
    if ($IncludeReleaseEnvironments) {
        $candidates += Get-ChildItem -LiteralPath $repoRoot -Directory -Force -Filter ".venv-release-*" -ErrorAction SilentlyContinue
    }
    if ($IncludeCanonicalRuntime) {
        $path = Join-Path $repoRoot ".runtime"
        if (Test-Path -LiteralPath $path) {
            $candidates += Get-Item -LiteralPath $path -Force
        }
    }
    if ($IncludePlanCaches) {
        $candidates += Get-ChildItem -LiteralPath (Join-Path $repoRoot "plans") -Directory -Recurse -Force -Filter "cache" -ErrorAction SilentlyContinue
    }
}

$candidates = @($candidates | Sort-Object FullName -Unique)
if ($candidates.Count -eq 0) {
    Write-Host "No generated directories matched the selected cleanup scope."
    exit 0
}

$totalBytes = 0L
foreach ($candidate in $candidates) {
    $safePath = Assert-SafeWorkspacePath -PathValue $candidate.FullName
    $size = Get-DirectorySizeBytes -PathValue $safePath
    $totalBytes += $size
    Write-Host ("{0,9:N2} GB  {1}" -f ($size / 1GB), $safePath)
}
Write-Host ("{0,9:N2} GB  total" -f ($totalBytes / 1GB))

if (-not $Apply) {
    Write-Host "Preview only. Re-run with -Apply to remove these generated directories."
    exit 0
}

foreach ($candidate in $candidates) {
    $safePath = Assert-SafeWorkspacePath -PathValue $candidate.FullName
    Write-Host "Removing $safePath"
    Remove-Item -LiteralPath $safePath -Recurse -Force
}
Write-Host "Workspace cleanup complete."
