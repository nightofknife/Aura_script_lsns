param(
    [Parameter(Mandatory = $true)]
    [string]$Repository,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^v\d+\.\d+\.\d+$')]
    [string]$ReleaseTag,
    [string]$BundleDirectory = "",
    [string]$ModelAsset = "",
    [string]$ChecksumAsset = "",
    [string]$PythonPath = "python"
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$contract = Get-Content -Raw -LiteralPath (Join-Path $repoRoot "packaging\release-contract.json") | ConvertFrom-Json
if (-not $BundleDirectory) { $BundleDirectory = $contract.assets.ocr.bundle_directory -replace '/', '\' }
if (-not $ModelAsset) { $ModelAsset = $contract.assets.ocr.model_asset }
if (-not $ChecksumAsset) { $ChecksumAsset = $contract.assets.ocr.checksum_asset }
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
        --title $ReleaseTag `
        --notes "Build-time OCR model asset for Aura Resonance release workflows." `
        --prerelease
    if ($LASTEXITCODE -ne 0) { throw "Failed to create OCR model release '$ReleaseTag'." }
}

gh release edit $ReleaseTag --repo $Repository --title $ReleaseTag
if ($LASTEXITCODE -ne 0) { throw "Failed to normalize OCR model release title for '$ReleaseTag'." }

gh release upload $ReleaseTag $zipPath $checksumPath --repo $Repository --clobber
if ($LASTEXITCODE -ne 0) { throw "Failed to upload OCR model release assets." }

Write-Host "Published OCR model release assets to $Repository@$ReleaseTag"
