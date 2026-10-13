#requires -Version 7.0
<#
.SYNOPSIS
Preview generated workspace files; pass -Apply to remove them.
.DESCRIPTION
Run from the repository root with PowerShell 7. Historical runtime cleanup keeps
unknown directories and the two most recent releases. User settings are backed
up under .cleanup before deletion; source files and research are not selected.
#>
param(
    [switch]$Apply,
    [switch]$PythonCachesOnly,
    [switch]$IncludeReleaseEnvironments,
    [switch]$IncludeCanonicalRuntime,
    [switch]$IncludePlanCaches,
    [switch]$IncludeHistoricalRuntime,
    [switch]$IncludeLegacyReleaseEnvironments,
    [switch]$IncludeTemporaryCaptures,
    [ValidateRange(1, 100)][int]$KeepRecentReleaseCount = 2,
    [Nullable[datetime]]$LogsBefore
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

function Assert-SafeWorkspacePath {
    param([string]$PathValue, [switch]$AllowMissing)
    $resolved = [System.IO.Path]::GetFullPath($PathValue)
    $prefix = $repoRoot.TrimEnd('\') + '\'
    if (-not $resolved.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to operate outside the repository: $resolved"
    }
    $ancestor = $resolved
    while ($ancestor -ne $repoRoot) {
        if (Test-Path -LiteralPath $ancestor) {
            $item = Get-Item -LiteralPath $ancestor -Force -ErrorAction Stop
            if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
                throw "Refusing to operate through a symbolic link or junction: $ancestor"
            }
        } elseif (-not $AllowMissing) {
            throw "Workspace path does not exist: $ancestor"
        }
        $ancestor = Split-Path -Parent $ancestor
    }
    return $resolved
}

function New-SafeWorkspaceDirectory {
    param([string]$PathValue)
    $safePath = Assert-SafeWorkspacePath -PathValue $PathValue -AllowMissing
    New-Item -ItemType Directory -Force -Path $safePath | Out-Null
    Assert-SafeWorkspacePath -PathValue $safePath | Out-Null
}

function Assert-CleanupTarget {
    param([string]$PathValue, [switch]$RefreshState)
    $safePath = Assert-SafeWorkspacePath -PathValue $PathValue
    $relative = [System.IO.Path]::GetRelativePath($repoRoot, $safePath).Replace('\', '/')
    if ($RefreshState -or $null -eq $script:cleanupTrackedPaths) {
        $script:cleanupTrackedPaths = @(& git -C $repoRoot ls-files)
        if ($LASTEXITCODE -ne 0) { throw "Cannot read Git tracked paths; cleanup aborted." }
        $script:cleanupProcesses = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object { $_.ProcessId -ne $PID })
    }
    foreach ($trackedPath in $script:cleanupTrackedPaths) {
        if ($trackedPath.Equals($relative, [System.StringComparison]::OrdinalIgnoreCase) -or
            $trackedPath.StartsWith($relative + '/', [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove a Git tracked file: $trackedPath"
        }
    }
    foreach ($process in $script:cleanupProcesses) {
        if (($process.ExecutablePath -and ($process.ExecutablePath -eq $safePath -or $process.ExecutablePath.StartsWith($safePath + '\', [System.StringComparison]::OrdinalIgnoreCase))) -or
            ($process.CommandLine -and $process.CommandLine.IndexOf($safePath, [System.StringComparison]::OrdinalIgnoreCase) -ge 0)) {
            throw "Cleanup target is used by process $($process.ProcessId) ($($process.Name)): $safePath"
        }
    }
    if ((Get-Item -LiteralPath $safePath -Force).PSIsContainer) {
        $links = @(Get-ChildItem -LiteralPath $safePath -Recurse -Force -Attributes ReparsePoint -ErrorAction Stop)
        if ($links.Count -gt 0) { throw "Cleanup target contains symbolic links or junctions: $safePath" }
    }
    return $safePath
}

function Get-DirectorySizeBytes {
    param([string]$PathValue)
    if (-not (Get-Item -LiteralPath $PathValue -Force).PSIsContainer) {
        return [int64](Get-Item -LiteralPath $PathValue -Force).Length
    }
    $measure = Get-ChildItem -LiteralPath $PathValue -File -Recurse -Force -ErrorAction Stop |
        Measure-Object -Property Length -Sum
    return [int64]($measure.Sum)
}

$candidates = @()
foreach ($sourceRootName in @("packages", "plans", "scripts", "tests", "tools", "packaging")) {
    $sourceRoot = Join-Path $repoRoot $sourceRootName
    if (Test-Path -LiteralPath $sourceRoot) {
        Assert-SafeWorkspacePath -PathValue $sourceRoot | Out-Null
        $candidates += Get-ChildItem -LiteralPath $sourceRoot -Directory -Recurse -Force -Filter "__pycache__" -ErrorAction SilentlyContinue
    }
}
$rootPythonCache = Join-Path $repoRoot "__pycache__"
if (Test-Path -LiteralPath $rootPythonCache) {
    $candidates += Get-Item -LiteralPath $rootPythonCache -Force
}
if (-not $PythonCachesOnly) {
    foreach ($name in @(".pytest_cache", ".pytest_tmp", ".test-tmp", "build", "dist")) {
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
    if ($IncludeHistoricalRuntime) {
        # Only known, reproducible output directories; never infer safety from .runtime-*.
        foreach ($name in @('.runtime-artifacts', '.runtime-build', '.runtime-cpu-local', '.runtime-gpu-local', '.runtime-gpu-build')) {
            $path = Join-Path $repoRoot $name
            if (Test-Path -LiteralPath $path) { $candidates += Get-Item -LiteralPath $path -Force }
        }
        $runtimeRoot = Join-Path $repoRoot ".runtime"
        if (-not $IncludeCanonicalRuntime -and (Test-Path -LiteralPath $runtimeRoot)) {
            Assert-SafeWorkspacePath -PathValue $runtimeRoot | Out-Null
            foreach ($name in @('build', 'artifacts')) {
                $path = Join-Path $runtimeRoot $name
                if (Test-Path -LiteralPath $path) { $candidates += Get-Item -LiteralPath $path -Force }
            }
            $releaseRoot = Join-Path $runtimeRoot "releases"
            if (Test-Path -LiteralPath $releaseRoot) {
                Assert-SafeWorkspacePath -PathValue $releaseRoot | Out-Null
                $candidates += Get-ChildItem -LiteralPath $releaseRoot -Directory -Force |
                    Where-Object { $_.Name -match '^v?\d+\.\d+\.\d+(?:-(?:cpu|gpu|overlay))?$' } |
                    Sort-Object LastWriteTime -Descending | Select-Object -Skip $KeepRecentReleaseCount
            }
        }
    }
    if ($IncludeLegacyReleaseEnvironments -and -not $IncludeReleaseEnvironments) {
        foreach ($name in @(".venv-release-cpu-local", ".venv-release-gpu-local", ".venv-release-gpu-build")) {
            $path = Join-Path $repoRoot $name
            if (Test-Path -LiteralPath $path) { $candidates += Get-Item -LiteralPath $path -Force }
        }
    }
    if ($IncludeTemporaryCaptures) {
        $candidates += Get-ChildItem -LiteralPath $repoRoot -File -Force -Filter ".tmp*.png"
        $candidates += Get-ChildItem -LiteralPath $repoRoot -Directory -Force -Filter ".tmp*"
        $specPath = Join-Path $repoRoot "AuraResonanceGui.spec"
        if (Test-Path -LiteralPath $specPath) { $candidates += Get-Item -LiteralPath $specPath -Force }
    }
    if ($null -ne $LogsBefore) {
        $logsRoot = Join-Path $repoRoot "logs"
        if (Test-Path -LiteralPath $logsRoot) {
            Assert-SafeWorkspacePath -PathValue $logsRoot | Out-Null
            $candidates += Get-ChildItem -LiteralPath $logsRoot -File -Recurse -Force |
                Where-Object { $_.LastWriteTime -lt $LogsBefore }
        }
    }
    if ($IncludePlanCaches) {
        $candidates += Get-ChildItem -LiteralPath (Join-Path $repoRoot "plans") -Directory -Recurse -Force -Filter "cache" -ErrorAction SilentlyContinue
    }
}

$candidates = @($candidates | Sort-Object FullName -Unique)
# Count each subtree once, even when two selected scopes overlap.
$selected = [System.Collections.Generic.List[object]]::new()
foreach ($candidate in $candidates) {
    $covered = $false
    foreach ($parent in $selected) {
        if ($candidate.FullName.StartsWith($parent.FullName + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
            $covered = $true
            break
        }
    }
    if (-not $covered) { $selected.Add($candidate) }
}
$candidates = @($selected.ToArray())
if ($candidates.Count -eq 0) {
    Write-Host "No generated files or directories matched the selected cleanup scope."
    exit 0
}

$manifest = [System.Collections.Generic.List[object]]::new()
$totalBytes = 0L
foreach ($candidate in $candidates) {
    $safePath = Assert-CleanupTarget -PathValue $candidate.FullName
    $relative = [System.IO.Path]::GetRelativePath($repoRoot, $safePath).Replace('\', '/')
    $size = Get-DirectorySizeBytes -PathValue $safePath
    $totalBytes += $size
    $manifest.Add([pscustomobject]@{ Path = $relative; Bytes = $size; Removed = $false })
    Write-Host ("{0,9:N2} GB  {1}" -f ($size / 1GB), $safePath)
}
Write-Host ("Selected {0} directories and {1} files." -f @($candidates | Where-Object PSIsContainer).Count, @($candidates | Where-Object { -not $_.PSIsContainer }).Count)
Write-Host ("{0,9:N2} GB  total" -f ($totalBytes / 1GB))

if (-not $Apply) {
    Write-Host "Preview only. Re-run with -Apply to remove the selected generated files."
    exit 0
}

# Preserve any settings created by a user running an old release before removing it.
$sessionId = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N')
$reportRoot = Join-Path (Join-Path $repoRoot '.cleanup') $sessionId
New-SafeWorkspaceDirectory -PathValue $reportRoot
$settingsRoot = Join-Path $reportRoot 'preserved-settings'
foreach ($candidate in $candidates | Where-Object PSIsContainer) {
    Assert-CleanupTarget -PathValue $candidate.FullName | Out-Null
    $settings = @(Get-ChildItem -LiteralPath $candidate.FullName -File -Recurse -Force |
        Where-Object { $_.FullName -match '[\\/]user-data[\\/]' -or $_.Name -in @('gui-settings.ini', 'gui-settings-layout-v2.ini') })
    foreach ($file in $settings) {
        $relative = [System.IO.Path]::GetRelativePath($repoRoot, $file.FullName)
        $destination = Join-Path $settingsRoot $relative
        Assert-SafeWorkspacePath -PathValue $file.FullName | Out-Null
        New-SafeWorkspaceDirectory -PathValue (Split-Path -Parent $destination)
        Assert-SafeWorkspacePath -PathValue $destination -AllowMissing | Out-Null
        Copy-Item -LiteralPath $file.FullName -Destination $destination -Force
    }
}
$report = [pscustomobject]@{ CreatedAt = (Get-Date -Format o); SelectedBytes = $totalBytes; PreservedSettings = $settingsRoot; Items = $manifest.ToArray() }
$reportPath = Join-Path $reportRoot 'cleanup.json'
if (Test-Path -LiteralPath $reportPath) { throw "Cleanup report already exists: $reportPath" }
$report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $reportPath -Encoding utf8
foreach ($candidate in $candidates) {
    $safePath = Assert-CleanupTarget -PathValue $candidate.FullName -RefreshState
    Write-Host "Removing $safePath"
    Remove-Item -LiteralPath $safePath -Recurse -Force
    $relative = [System.IO.Path]::GetRelativePath($repoRoot, $safePath).Replace('\', '/')
    ($manifest | Where-Object Path -EQ $relative).Removed = $true
    $report.Items = $manifest.ToArray()
    $report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $reportPath -Encoding utf8
}
Write-Host "Workspace cleanup complete. Report: $reportPath"
