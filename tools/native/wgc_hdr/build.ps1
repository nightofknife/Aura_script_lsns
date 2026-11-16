$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
$scope = Join-Path $repo '.pytest_tmp/wgc-hdr-native-build'
$tmpDir = (New-Item -ItemType Directory -Force -Path (Join-Path $scope 'tmp')).FullName
$oldTemp = $env:TEMP; $oldTmp = $env:TMP; $oldTmpDir = $env:TMPDIR
$env:TEMP = $tmpDir; $env:TMP = $tmpDir; $env:TMPDIR = $tmpDir
Push-Location $repo
try {
    cmake -S tools/native/wgc_hdr -B (Join-Path $scope 'build') -G 'Visual Studio 17 2022' -A x64
    if ($LASTEXITCODE -ne 0) { throw 'Native WGC configure failed.' }
    cmake --build (Join-Path $scope 'build') --config Release --parallel 2
    if ($LASTEXITCODE -ne 0) { throw 'Native WGC build failed.' }
    $assetDir = (New-Item -ItemType Directory -Force -Path 'plans/aura_base/assets/windows').FullName
    Copy-Item -LiteralPath (Join-Path $scope 'build/Release/aura_wgc_hdr.dll') -Destination $assetDir -Force
} finally {
    Pop-Location
    $env:TEMP = $oldTemp; $env:TMP = $oldTmp; $env:TMPDIR = $oldTmpDir
}
