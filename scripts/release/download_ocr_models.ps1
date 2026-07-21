param(
    [Parameter(Mandatory = $true)]
    [string]$Repository,
    [string]$ReleaseTag = "model-ppocrv5-server-v1",
    [string]$ModelAsset = "models-ocr-ppocrv5_server-v1.zip",
    [string]$ChecksumAsset = "models-ocr-ppocrv5_server-v1.sha256",
    [string]$DestinationRoot = "models\ocr",
    [string]$WorkDirectory = ".runtime-model-assets",
    [string]$PythonPath = "python"
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$workRoot = Join-Path $repoRoot $WorkDirectory
$destination = Join-Path $repoRoot $DestinationRoot
$validator = Join-Path $repoRoot "scripts\release\validate_ocr_bundle.py"

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
$bundleCandidates = @(Get-ChildItem -LiteralPath $extractRoot -Directory -Recurse -Filter "ppocrv5_server")
if ($bundleCandidates.Count -ne 1) {
    throw "OCR model archive must contain exactly one ppocrv5_server directory; found $($bundleCandidates.Count)."
}

$destinationBundle = Join-Path $destination "ppocrv5_server"
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
