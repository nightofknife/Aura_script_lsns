param(
    [string]$CMake = 'cmake',
    [string]$Ninja = 'ninja',
    [string]$ToolchainBin = '',
    [string]$BuildDirectory = ''
)
$ErrorActionPreference = 'Stop'
$bridgeRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$planRoot = [IO.Path]::GetFullPath((Join-Path $bridgeRoot '..'))
$repoRoot = [IO.Path]::GetFullPath((Join-Path $planRoot '../..'))
if (-not $BuildDirectory) { $BuildDirectory = Join-Path $repoRoot '.pytest_tmp/bridge_native_build' }
$BuildDirectory = [IO.Path]::GetFullPath($BuildDirectory)
if (-not $BuildDirectory.StartsWith($repoRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'BuildDirectory must be inside this repository.'
}
New-Item -ItemType Directory -Force -Path $BuildDirectory | Out-Null
$savedEnvironment = @{ TEMP=$env:TEMP; TMP=$env:TMP; TMPDIR=$env:TMPDIR; PATH=$env:PATH }
try {
    $env:TEMP=$BuildDirectory; $env:TMP=$BuildDirectory; $env:TMPDIR=$BuildDirectory
    $configure = @('-S', $bridgeRoot, '-B', $BuildDirectory, '-G', 'Ninja', '-DCMAKE_BUILD_TYPE=Release', "-DCMAKE_MAKE_PROGRAM=$Ninja")
    if ($ToolchainBin) {
        $ToolchainBin = [IO.Path]::GetFullPath($ToolchainBin)
        $env:PATH = $ToolchainBin + ';' + $env:PATH
        $configure += @("-DCMAKE_C_COMPILER=$ToolchainBin/x86_64-w64-mingw32-clang.exe", "-DCMAKE_CXX_COMPILER=$ToolchainBin/x86_64-w64-mingw32-clang++.exe")
    }
    Push-Location $repoRoot
    try {
        & $CMake @configure
        if ($LASTEXITCODE) { throw "CMake configure failed: $LASTEXITCODE" }
        & $CMake --build $BuildDirectory --parallel 4
        if ($LASTEXITCODE) { throw "Native build failed: $LASTEXITCODE" }
        $runtimeRoot = Join-Path $planRoot 'assets/input_bridge'
        & $CMake --install $BuildDirectory --prefix $runtimeRoot --component Runtime
        if ($LASTEXITCODE) { throw "Native install failed: $LASTEXITCODE" }
        $licenseRoot = Join-Path $runtimeRoot 'licenses'
        New-Item -ItemType Directory -Force -Path $licenseRoot | Out-Null
        Copy-Item -LiteralPath (Join-Path $BuildDirectory '_deps/minhook-src/LICENSE.txt') -Destination (Join-Path $licenseRoot 'MinHook.txt')
        Copy-Item -LiteralPath (Join-Path $BuildDirectory '_deps/json-src/LICENSE.MIT') -Destination (Join-Path $licenseRoot 'nlohmann-json.txt')
        Write-Output "Bridge runtime installed: $runtimeRoot"
    } finally { Pop-Location }
} finally {
    $env:TEMP=$savedEnvironment.TEMP; $env:TMP=$savedEnvironment.TMP; $env:TMPDIR=$savedEnvironment.TMPDIR; $env:PATH=$savedEnvironment.PATH
}
