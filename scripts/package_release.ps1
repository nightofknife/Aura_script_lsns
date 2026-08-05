param(
    [ValidateSet("cpu", "gpu")]
    [string]$Profile = "cpu",
    [string]$BasePython = "",
    [string]$VenvPath = "",
    [string]$RuntimeRoot = "",
    [string]$ReleaseName = "",
    [switch]$RefreshDependencies,
    [switch]$RecreateEnvironment,
    [switch]$SkipBuild,
    [switch]$NoZip,
    [switch]$IncludeNvidia,
    [switch]$CreateNvidiaOverlay
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

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
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($ArgumentList -join ' ')"
    }
    return $output
}

function Resolve-RepoPath {
    param([string]$PathValue, [string]$Label)

    $candidate = $PathValue
    if (-not [System.IO.Path]::IsPathRooted($candidate)) {
        $candidate = Join-Path $repoRoot $candidate
    }
    $resolved = [System.IO.Path]::GetFullPath($candidate)
    $repoPrefix = $repoRoot.TrimEnd('\') + '\'
    if (-not $resolved.StartsWith($repoPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must stay inside the repository: $resolved"
    }
    return $resolved
}

function Resolve-BasePython {
    if ($BasePython) {
        return $BasePython
    }
    $resolved = Invoke-CheckedCommand `
        -FilePath "py" `
        -ArgumentList @("-3.12", "-c", "import sys; print(sys.executable)") `
        -CaptureOutput
    return $resolved.Trim()
}

function Get-RequirementsFingerprint {
    param([string[]]$Paths)
    return (($Paths | ForEach-Object {
        (Get-FileHash -LiteralPath $_ -Algorithm SHA256).Hash
    }) -join "`n")
}

function Get-RelativeRepoPath {
    param([string]$PathValue)
    $baseUri = New-Object System.Uri(($repoRoot.TrimEnd('\') + '\'))
    $targetUri = New-Object System.Uri([System.IO.Path]::GetFullPath($PathValue))
    return [System.Uri]::UnescapeDataString($baseUri.MakeRelativeUri($targetUri).ToString()).Replace('/', '\')
}

if (-not $VenvPath) {
    $VenvPath = ".venv-release-$Profile"
}
if (-not $RuntimeRoot) {
    $RuntimeRoot = ".runtime\packages\$Profile"
}
if (-not $ReleaseName) {
    $ReleaseName = "AuraResonance-local-win-x64-$Profile"
}

$venvRoot = Resolve-RepoPath -PathValue $VenvPath -Label "Release environment"
$runtimeRootPath = Resolve-RepoPath -PathValue $RuntimeRoot -Label "Runtime output"
$expectedVenvPrefix = ".venv-release-"
if (-not (Split-Path $venvRoot -Leaf).StartsWith($expectedVenvPrefix)) {
    throw "Release environment directory must start with '$expectedVenvPrefix': $venvRoot"
}

Push-Location $repoRoot
try {
    if ($RecreateEnvironment -and (Test-Path -LiteralPath $venvRoot)) {
        Write-Host "Removing release environment: $venvRoot"
        Remove-Item -LiteralPath $venvRoot -Recurse -Force
    }

    $basePythonPath = Resolve-BasePython
    $version = Invoke-CheckedCommand `
        -FilePath $basePythonPath `
        -ArgumentList @("-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')") `
        -CaptureOutput
    if (-not $version.StartsWith("3.12.")) {
        throw "Release builds require Python 3.12.x. Current: $version"
    }

    if (-not (Test-Path -LiteralPath $venvRoot)) {
        Write-Host "Creating isolated $Profile release environment: $venvRoot"
        Invoke-CheckedCommand -FilePath $basePythonPath -ArgumentList @("-m", "venv", "--copies", $venvRoot)
    }

    $venvPython = Join-Path $venvRoot "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $venvPython)) {
        throw "Release environment is incomplete: $venvPython"
    }

    $releaseRequirements = Join-Path $repoRoot "requirements\release-$Profile.txt"
    $runtimeLock = Join-Path $repoRoot "requirements\runtime.lock"
    $stampPath = Join-Path $venvRoot ".aura-release-requirements"
    $fingerprint = Get-RequirementsFingerprint -Paths @($releaseRequirements, $runtimeLock)
    $installedFingerprint = if (Test-Path -LiteralPath $stampPath) {
        (Get-Content -Raw -LiteralPath $stampPath).Trim()
    } else {
        ""
    }

    if ($RefreshDependencies -or $installedFingerprint -ne $fingerprint) {
        Write-Host "Installing pinned $Profile release dependencies ..."
        Invoke-CheckedCommand -FilePath $venvPython -ArgumentList @(
            "-m", "pip", "install", "-r", $releaseRequirements
        )
        Set-Content -LiteralPath $stampPath -Value $fingerprint -Encoding ASCII
    } else {
        Write-Host "Pinned release dependencies are unchanged; reusing $venvRoot"
    }

    $env:PYTHONNOUSERSITE = "1"
    Invoke-CheckedCommand -FilePath $venvPython -ArgumentList @("-m", "pip", "check")

    $mumuScript = Join-Path $repoRoot "scripts\fetch_mumu_runtime_assets.py"
    & $venvPython $mumuScript --check
    if ($LASTEXITCODE -ne 0) {
        Write-Host "MuMu runtime assets are missing; fetching them now ..."
        Invoke-CheckedCommand -FilePath $venvPython -ArgumentList @($mumuScript)
    }

    $ocrBundle = Join-Path $repoRoot "models\ocr\ppocrv5_server"
    $ocrValidator = Join-Path $repoRoot "scripts\release\validate_ocr_bundle.py"
    if (-not (Test-Path -LiteralPath $ocrBundle)) {
        throw "OCR model bundle not found: $ocrBundle. See docs/project-reference/release-packaging.md."
    }
    Invoke-CheckedCommand -FilePath $venvPython -ArgumentList @($ocrValidator, $ocrBundle)

    $relativeVenvPython = Get-RelativeRepoPath -PathValue $venvPython
    $relativeRuntimeRoot = Get-RelativeRepoPath -PathValue $runtimeRootPath
    $buildArgs = @{
        VenvPython = $relativeVenvPython
        RuntimeRoot = $relativeRuntimeRoot
        ReleaseName = $ReleaseName
        OnnxRuntimeProfile = $Profile
        IncludeGui = $true
        CreateZip = -not $NoZip
        IncludeNvidia = $IncludeNvidia
        CreateNvidiaOverlay = $CreateNvidiaOverlay
        SkipBuild = $SkipBuild
    }

    & (Join-Path $PSScriptRoot "build_release.ps1") @buildArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Release build failed with exit code $LASTEXITCODE."
    }

    Write-Host ""
    Write-Host "Release ready under: $(Join-Path $runtimeRootPath 'release')"
}
finally {
    Pop-Location
}
