param(
    [string]$Repository = "",
    [string]$ReleaseTag = "",
    [string]$ModelAsset = "",
    [string]$ChecksumAsset = "",
    [string]$DestinationRoot = "models\ocr",
    [string]$WorkDirectory = ".runtime-model-assets",
    [string]$Proxy = "",
    [switch]$Offline,
    [string]$PythonPath = "python"
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$contract = Get-Content -Raw -LiteralPath (Join-Path $repoRoot "packaging\release-contract.json") | ConvertFrom-Json
if ($contract.assets.ocr.source -eq "official_onnx" -and -not $ReleaseTag) {
    $prepareArguments = @(
        "-m", "tools.prepare_ocr_models",
        "--manifest", (Join-Path $repoRoot $contract.assets.ocr.source_manifest),
        "--destination-root", (Join-Path $repoRoot $DestinationRoot)
    )
    if ($Proxy) { $prepareArguments += @("--proxy", $Proxy) }
    if ($Offline) { $prepareArguments += "--offline" }
    Push-Location $repoRoot
    try {
        & $PythonPath @prepareArguments
        if ($LASTEXITCODE -ne 0) { throw "Failed to prepare official ONNX OCR models." }
    }
    finally {
        Pop-Location
    }
    return
}
if (-not $ReleaseTag) { $ReleaseTag = $contract.assets.ocr.release_tag }
if (-not $ModelAsset) { $ModelAsset = $contract.assets.ocr.model_asset }
if (-not $ChecksumAsset) { $ChecksumAsset = $contract.assets.ocr.checksum_asset }
$workRoot = Join-Path $repoRoot $WorkDirectory
$destination = Join-Path $repoRoot $DestinationRoot
$validator = Join-Path $repoRoot "scripts\release\validate_ocr_bundle.py"
$bundleName = Split-Path -Leaf $contract.assets.ocr.bundle_directory
if (-not $ReleaseTag) { throw "A model release tag is required for GitHub Release downloads." }
if (-not $Repository) { throw "A repository is required for GitHub Release downloads." }

if (Test-Path -LiteralPath $workRoot) {
    Remove-Item -LiteralPath $workRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $workRoot | Out-Null

gh release download $ReleaseTag `
    --repo $Repository `
    --dir $workRoot `
    --pattern $ModelAsset `
    --pattern $ChecksumAsset `
    --clobber
if ($LASTEXITCODE -ne 0) {
    throw "Failed to download OCR model assets from release '$ReleaseTag'."
}

$zipPath = Join-Path $workRoot $ModelAsset
$checksumPath = Join-Path $workRoot $ChecksumAsset
if (-not (Test-Path -LiteralPath $zipPath)) { throw "Missing downloaded model archive: $zipPath" }
if (-not (Test-Path -LiteralPath $checksumPath)) { throw "Missing downloaded checksum: $checksumPath" }

$checksumText = (Get-Content -Raw -LiteralPath $checksumPath).Trim()
if ($checksumText -notmatch "(?i)\b([a-f0-9]{64})\b") {
    throw "Could not parse SHA256 from $checksumPath"
}
$expectedHash = $Matches[1].ToLowerInvariant()
$actualHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualHash -ne $expectedHash) {
    throw "OCR model hash mismatch. Expected $expectedHash but got $actualHash."
}

$extractRoot = Join-Path $workRoot "extract"
Expand-Archive -LiteralPath $zipPath -DestinationPath $extractRoot -Force
$bundleCandidates = @(Get-ChildItem -LiteralPath $extractRoot -Directory -Recurse -Filter $bundleName)
if ($bundleCandidates.Count -ne 1) {
    throw "OCR model archive must contain exactly one $bundleName directory; found $($bundleCandidates.Count)."
}

$destinationBundle = Join-Path $destination $bundleName
if (Test-Path -LiteralPath $destinationBundle) {
    Remove-Item -LiteralPath $destinationBundle -Recurse -Force
}
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destinationBundle) | Out-Null
Copy-Item -LiteralPath $bundleCandidates[0].FullName -Destination $destinationBundle -Recurse -Force

& $PythonPath $validator $destinationBundle
if ($LASTEXITCODE -ne 0) {
    throw "Downloaded OCR model bundle failed validation."
}

Write-Host "OCR model bundle is ready: $destinationBundle"
