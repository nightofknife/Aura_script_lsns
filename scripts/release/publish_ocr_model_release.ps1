param(
    [Parameter(Mandatory = $true)]
    [string]$Repository,
    [string]$ReleaseTag = "model-ppocrv5-server-v1",
    [string]$BundleDirectory = "models\ocr\ppocrv5_server",
    [string]$ModelAsset = "models-ocr-ppocrv5_server-v1.zip",
    [string]$ChecksumAsset = "models-ocr-ppocrv5_server-v1.sha256",
    [string]$PythonPath = "python"
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$bundle = Join-Path $repoRoot $BundleDirectory
$validator = Join-Path $repoRoot "scripts\release\validate_ocr_bundle.py"
$outputRoot = Join-Path $repoRoot ".runtime-model-release"
$zipPath = Join-Path $outputRoot $ModelAsset
$checksumPath = Join-Path $outputRoot $ChecksumAsset

& $PythonPath $validator $bundle
if ($LASTEXITCODE -ne 0) { throw "Local OCR model bundle failed validation." }

if (Test-Path -LiteralPath $outputRoot) {
    Remove-Item -LiteralPath $outputRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $outputRoot | Out-Null
Compress-Archive -LiteralPath $bundle -DestinationPath $zipPath -CompressionLevel Optimal -Force
$hash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
"$hash  $ModelAsset" | Set-Content -LiteralPath $checksumPath -Encoding ascii

gh release view $ReleaseTag --repo $Repository *> $null
if ($LASTEXITCODE -ne 0) {
    gh release create $ReleaseTag `
        --repo $Repository `
        --title "PP-OCRv5 Server ONNX model bundle v1" `
        --notes "Build-time OCR model asset for Aura Resonance release workflows." `
        --prerelease
    if ($LASTEXITCODE -ne 0) { throw "Failed to create OCR model release '$ReleaseTag'." }
}

gh release upload $ReleaseTag $zipPath $checksumPath --repo $Repository --clobber
if ($LASTEXITCODE -ne 0) { throw "Failed to upload OCR model release assets." }

Write-Host "Published OCR model release assets to $Repository@$ReleaseTag"
