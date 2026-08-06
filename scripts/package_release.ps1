param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("cpu", "gpu", "overlay", "all")]
    [string]$Profile,
    [Parameter(Mandatory = $true)]
    [string]$ReleaseLabel,
    [string]$BasePython = "",
    [string]$OutputRoot = "",
    [string]$AssetRepository = "",
    [switch]$RefreshDependencies,
    [switch]$RecreateEnvironment,
    [switch]$RefreshAssets,
    [switch]$Offline,
    [switch]$AllowDirty
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$contractPath = Join-Path $repoRoot "packaging\release-contract.json"
$contract = Get-Content -Raw -LiteralPath $contractPath | ConvertFrom-Json

function Invoke-CheckedCommand {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
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
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($ArgumentList -join ' ')"
    }
    return $output
}

function Resolve-RepoPath {
    param([string]$PathValue, [string]$Label)
    $candidate = if ([System.IO.Path]::IsPathRooted($PathValue)) { $PathValue } else { Join-Path $repoRoot $PathValue }
    $resolved = [System.IO.Path]::GetFullPath($candidate)
    $prefix = $repoRoot.TrimEnd('\') + '\'
    if (-not $resolved.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must stay inside the repository: $resolved"
    }
    return $resolved
}

function Get-RelativeRepoPath {
    param([string]$PathValue)
    $baseUri = [System.Uri]::new($repoRoot.TrimEnd('\') + '\')
    $targetUri = [System.Uri]::new([System.IO.Path]::GetFullPath($PathValue))
    return [System.Uri]::UnescapeDataString($baseUri.MakeRelativeUri($targetUri).ToString()).Replace('/', '\')
}

function Resolve-BasePython {
    if ($BasePython) { return $BasePython }
    $selector = "-$($contract.python.major_minor)"
    $resolved = Invoke-CheckedCommand -FilePath "py" -ArgumentList @($selector, "-c", "import sys; print(sys.executable)") -CaptureOutput
    return $resolved.Trim()
}

function Resolve-AssetRepository {
    if ($AssetRepository) { return $AssetRepository }
    if ($env:GITHUB_REPOSITORY) { return $env:GITHUB_REPOSITORY }
    $origin = (Invoke-CheckedCommand -FilePath "git" -ArgumentList @("remote", "get-url", "origin") -CaptureOutput).Trim()
    if ($origin -match 'github\.com[/:]([^/]+/[^/]+?)(?:\.git)?$') { return $Matches[1] }
    throw "Could not infer the GitHub asset repository. Pass -AssetRepository owner/repository."
}

function Ensure-ReleaseEnvironment {
    param([string]$SelectedProfile, [string]$PythonPath)

    $profileContract = $contract.profiles.$SelectedProfile
    $lockPath = Join-Path $repoRoot ($profileContract.requirements_lock -replace '/', '\')
    if (-not (Test-Path -LiteralPath $lockPath)) { throw "Release lock not found: $lockPath" }
    $venvRoot = Join-Path $repoRoot ".venv-release-$SelectedProfile"
    if ($RecreateEnvironment -and (Test-Path -LiteralPath $venvRoot)) {
        Remove-Item -LiteralPath $venvRoot -Recurse -Force
    }
    $venvPython = Join-Path $venvRoot "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $venvPython)) {
        Invoke-CheckedCommand -FilePath $PythonPath -ArgumentList @("-m", "venv", "--copies", $venvRoot)
    }

    $stamp = Join-Path $venvRoot ".aura-release-lock"
    $fingerprint = (Get-FileHash -LiteralPath $lockPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $installedFingerprint = if (Test-Path -LiteralPath $stamp) { (Get-Content -Raw -LiteralPath $stamp).Trim() } else { "" }
    if ($RefreshDependencies -or $installedFingerprint -ne $fingerprint) {
        Invoke-CheckedCommand -FilePath $venvPython -ArgumentList @(
            "-m", "pip", "install", "--disable-pip-version-check", "--require-hashes", "-r", $lockPath
        )
        Set-Content -LiteralPath $stamp -Value $fingerprint -Encoding ASCII
    }
    $env:PYTHONNOUSERSITE = "1"
    Invoke-CheckedCommand -FilePath $venvPython -ArgumentList @("-m", "pip", "check")
    Invoke-CheckedCommand -FilePath $venvPython -ArgumentList @(
        "-m", "scripts.release.verify_release_environment", $lockPath
    )
    return [pscustomobject]@{ Root = $venvRoot; Python = $venvPython; Lock = $lockPath }
}

function Ensure-ReleaseAssets {
    param([string]$PythonPath)

    $mumuLock = Join-Path $repoRoot ($contract.assets.mumu_lock -replace '/', '\')
    $mumuArgs = @("scripts\fetch_mumu_runtime_assets.py", "--lock-file", $mumuLock)
    if ($RefreshAssets) { $mumuArgs += "--force" }
    if ($Offline) {
        Invoke-CheckedCommand -FilePath $PythonPath -ArgumentList @("scripts\fetch_mumu_runtime_assets.py", "--check", "--lock-file", $mumuLock)
    } else {
        Invoke-CheckedCommand -FilePath $PythonPath -ArgumentList $mumuArgs
    }

    $ocrBundle = Join-Path $repoRoot ($contract.assets.ocr.bundle_directory -replace '/', '\')
    $ocrValidator = Join-Path $repoRoot "scripts\release\validate_ocr_bundle.py"
    $ocrValid = $false
    if ((Test-Path -LiteralPath $ocrBundle) -and -not $RefreshAssets) {
        & $PythonPath $ocrValidator $ocrBundle *> $null
        $ocrValid = $LASTEXITCODE -eq 0
    }
    if (-not $ocrValid) {
        if ($Offline) { throw "OCR model bundle is missing or invalid while -Offline is active: $ocrBundle" }
        $repository = Resolve-AssetRepository
        & pwsh -NoProfile -File (Join-Path $repoRoot "scripts\release\download_ocr_models.ps1") `
            -Repository $repository `
            -ReleaseTag $contract.assets.ocr.release_tag `
            -ModelAsset $contract.assets.ocr.model_asset `
            -ChecksumAsset $contract.assets.ocr.checksum_asset `
            -PythonPath $PythonPath
        if ($LASTEXITCODE -ne 0) { throw "OCR model download failed." }
    }
    Invoke-CheckedCommand -FilePath $PythonPath -ArgumentList @($ocrValidator, $ocrBundle)
}

function New-ReleaseArchive {
    param([string]$SourceRoot, [string]$Destination)
    if (Test-Path -LiteralPath $Destination) { Remove-Item -LiteralPath $Destination -Force }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Destination) | Out-Null
    Compress-Archive -LiteralPath $SourceRoot -DestinationPath $Destination -CompressionLevel Optimal
}

$safeLabel = $ReleaseLabel -replace '[^A-Za-z0-9._-]', '-'
if (-not $safeLabel) { throw "Release label is empty after sanitization." }
if (-not $OutputRoot) { $OutputRoot = ".runtime\releases\$safeLabel" }
$outputRootPath = Resolve-RepoPath -PathValue $OutputRoot -Label "Release output"
$basePythonPath = Resolve-BasePython
$pythonVersion = (Invoke-CheckedCommand -FilePath $basePythonPath -ArgumentList @(
    "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"
) -CaptureOutput).Trim()
if (-not $pythonVersion.StartsWith("$($contract.python.major_minor).")) {
    throw "Release builds require Python $($contract.python.major_minor).x. Current: $pythonVersion"
}

Push-Location $repoRoot
try {
    $sourceCommit = (Invoke-CheckedCommand -FilePath "git" -ArgumentList @("rev-parse", "HEAD") -CaptureOutput).Trim()
    $sourceStatus = (Invoke-CheckedCommand -FilePath "git" -ArgumentList @("status", "--porcelain", "--untracked-files=normal") -CaptureOutput) -join "`n"
    $sourceDirty = -not [string]::IsNullOrWhiteSpace($sourceStatus)
    if ($sourceDirty -and -not $AllowDirty) {
        throw "Release source tree is dirty. Commit the changes or pass -AllowDirty for a local test build."
    }
    $dirtyValue = if ($sourceDirty) { "true" } else { "false" }
    $profiles = if ($Profile -eq "all") { @("cpu", "gpu", "overlay") } else { @($Profile) }
    $archives = @{}

    foreach ($selectedProfile in $profiles) {
        Write-Host "Preparing Aura release profile: $selectedProfile"
        $environment = Ensure-ReleaseEnvironment -SelectedProfile $selectedProfile -PythonPath $basePythonPath
        $profileContract = $contract.profiles.$selectedProfile
        $runtimeRoot = Join-Path $repoRoot ".runtime\build\$safeLabel\$selectedProfile"
        $relativeRuntimeRoot = Get-RelativeRepoPath -PathValue $runtimeRoot
        $relativeVenvPython = Get-RelativeRepoPath -PathValue $environment.Python

        if ($selectedProfile -eq "overlay") {
            $releaseName = $profileContract.release_directory.Replace("{label}", $safeLabel)
            & (Join-Path $PSScriptRoot "build_release.ps1") `
                -VenvPython $relativeVenvPython `
                -RuntimeRoot $relativeRuntimeRoot `
                -ReleaseName $releaseName `
                -OnnxRuntimeProfile gpu `
                -SkipBuild `
                -SkipAssemble `
                -CreateNvidiaOverlay
            if ($LASTEXITCODE -ne 0) { throw "Overlay assembly failed." }
            $releaseRoot = Join-Path $runtimeRoot "release\$releaseName-nvidia-overlay\$releaseName"
        } else {
            Ensure-ReleaseAssets -PythonPath $environment.Python
            $releaseName = $profileContract.release_directory.Replace("{label}", $safeLabel)
            & (Join-Path $PSScriptRoot "build_release.ps1") `
                -VenvPython $relativeVenvPython `
                -RuntimeRoot $relativeRuntimeRoot `
                -ReleaseName $releaseName `
                -OnnxRuntimeProfile $selectedProfile `
                -IncludeGui
            if ($LASTEXITCODE -ne 0) { throw "Release build failed for $selectedProfile." }
            $releaseRoot = Join-Path $runtimeRoot "release\$releaseName"
        }

        $infoArgs = @(
            "-m", "scripts.release.write_build_info",
            "--root", $releaseRoot,
            "--profile", $selectedProfile,
            "--label", $safeLabel,
            "--source-commit", $sourceCommit,
            "--source-dirty", $dirtyValue,
            "--contract", $contractPath,
            "--lock", $environment.Lock
        )
        if ($selectedProfile -ne "overlay") {
            $infoArgs += @(
                "--ocr-root", (Join-Path $repoRoot $contract.assets.ocr.bundle_directory),
                "--mumu-lock", (Join-Path $repoRoot $contract.assets.mumu_lock)
            )
        }
        Invoke-CheckedCommand -FilePath $environment.Python -ArgumentList $infoArgs

        $validateArgs = @(
            "-m", "scripts.release.validate_release",
            "--profile", $selectedProfile,
            "--label", $safeLabel,
            "--release-root", $releaseRoot,
            "--contract", $contractPath
        )
        if ($selectedProfile -ne "overlay") { $validateArgs += "--runtime-smoke" }
        Invoke-CheckedCommand -FilePath $environment.Python -ArgumentList $validateArgs
        if ($selectedProfile -ne "overlay") {
            Invoke-CheckedCommand -FilePath $environment.Python -ArgumentList @(
                "scripts\release\prune_release_payload.py", "--release-root", $releaseRoot
            )
            Invoke-CheckedCommand -FilePath $environment.Python -ArgumentList @(
                "-m", "scripts.release.validate_release", "--profile", $selectedProfile, "--label", $safeLabel,
                "--release-root", $releaseRoot, "--contract", $contractPath
            )
        }

        $artifactName = $contract.artifacts.$selectedProfile.Replace("{label}", $safeLabel)
        $archivePath = Join-Path $outputRootPath $artifactName
        New-ReleaseArchive -SourceRoot $releaseRoot -Destination $archivePath
        Invoke-CheckedCommand -FilePath $environment.Python -ArgumentList @(
            "-m", "scripts.release.validate_release", "--profile", $selectedProfile, "--label", $safeLabel,
            "--release-root", $releaseRoot, "--archive", $archivePath, "--contract", $contractPath
        )
        $archives[$selectedProfile] = $archivePath
        Write-Host "Release artifact ready: $archivePath"
    }

    if ($Profile -eq "all") {
        $setArgs = @(
            "-m", "scripts.release.validate_release_set",
            "--cpu", $archives.cpu,
            "--gpu", $archives.gpu,
            "--overlay", $archives.overlay,
            "--label", $safeLabel,
            "--contract", $contractPath,
            "--work-root", (Join-Path $repoRoot ".runtime\validate-set\$safeLabel"),
            "--checksums", (Join-Path $outputRootPath $contract.artifacts.checksums)
        )
        if ($AllowDirty) { $setArgs += "--allow-dirty" }
        Invoke-CheckedCommand -FilePath $basePythonPath -ArgumentList $setArgs
    }
    Write-Host "Aura release output: $outputRootPath"
}
finally {
    Pop-Location
}
