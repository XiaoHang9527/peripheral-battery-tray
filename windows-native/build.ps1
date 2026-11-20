param(
    [string]$Compiler = 'clang++',
    [string]$OutputDirectory,
    [switch]$Test
)

$ErrorActionPreference = 'Stop'
$output = if ($OutputDirectory) { $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputDirectory) }
          else { Join-Path $PSScriptRoot 'dist' }
New-Item -Path $output -ItemType Directory -Force | Out-Null
$compilerPath = (Get-Command $Compiler -ErrorAction Stop).Source
$windresPath = Join-Path (Split-Path -Parent $compilerPath) 'x86_64-w64-mingw32-windres.exe'
if (-not (Test-Path -LiteralPath $windresPath)) {
    $windresPath = (Get-Command windres -ErrorAction Stop).Source
}
$version = [regex]::Match((Get-Content (Join-Path $PSScriptRoot 'update.hpp') -Raw), 'version = L"([0-9]+\.[0-9]+\.[0-9]+)"').Groups[1].Value
if (-not $version) { throw 'Expected X.Y.Z in update.hpp.' }
$sources = @('main.cpp', 'devices.cpp', 'bluetooth.cpp', 'graphics.cpp', 'preferences.cpp', 'update.cpp') | ForEach-Object {
    Join-Path $PSScriptRoot $_
}
$flags = @(
    '-std=c++20', '-O2', '-Wall', '-Wextra', '-Wpedantic', '-Werror',
    '-DUNICODE', '-D_UNICODE', '-DNOMINMAX', '-DWIN32_LEAN_AND_MEAN', '-D_WIN32_WINNT=0x0A00'
)
$libraries = @('-lhid', '-lsetupapi', '-lbthprops', '-lgdiplus', '-lcomctl32', '-luxtheme', '-lshell32', '-lole32', '-luuid', '-lwinhttp', '-lbcrypt', '-ladvapi32')
$name = Join-Path $output 'peripheral-battery-tray-native.exe'
$build = Join-Path $output ('.native-build-' + [guid]::NewGuid().ToString('N'))
New-Item -Path $build -ItemType Directory | Out-Null
$temp = $env:TEMP
$tmp = $env:TMP
try {
    $env:TEMP = $build
    $env:TMP = $build
    $candidate = Join-Path $build 'peripheral-battery-tray-native.exe'
    $resources = Join-Path $build 'app.o'
    $resourceScript = Join-Path $build 'app.rc'
    $template = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'app.rc') -Raw
    $template.Replace('@VERSION@', $version).Replace('@FILE_VERSION@', ($version.Replace('.', ',') + ',0')) |
        Set-Content -LiteralPath $resourceScript -Encoding ascii
    & $windresPath --target=pe-x86-64 "-I$PSScriptRoot" $resourceScript $resources
    if ($LASTEXITCODE -ne 0) { throw "Windows resource build failed ($LASTEXITCODE)." }
    & $compilerPath @flags @sources $resources -static -s -municode -mwindows -o $candidate @libraries
    if ($LASTEXITCODE -ne 0) { throw "Native build failed ($LASTEXITCODE)." }
    if ($Test) {
        $tests = Join-Path $build 'protocol-tests.exe'
        & $compilerPath @flags (Join-Path $PSScriptRoot 'tests.cpp') -static -o $tests
        if ($LASTEXITCODE -ne 0) { throw "Protocol test build failed ($LASTEXITCODE)." }
        & $tests
        if ($LASTEXITCODE -ne 0) { throw "Protocol tests failed ($LASTEXITCODE)." }
    }
    Move-Item -LiteralPath $candidate -Destination $name -Force
    $jsonSource = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'third_party\picojson.h') -Raw
    $notice = [regex]::Match($jsonSource, '(?s)^/\*(.*?)\*/').Groups[1].Value
    $notice -replace '(?m)^ \* ?','' |
        Set-Content -LiteralPath (Join-Path $output 'PicoJSON-LICENSE.txt') -Encoding utf8
    Get-Item -LiteralPath $name | Select-Object FullName, Length
} finally {
    $env:TEMP = $temp
    $env:TMP = $tmp
    # Only files produced directly inside this private build directory are temporary.
    Get-ChildItem -LiteralPath $build -File | Remove-Item -Force
    Remove-Item -LiteralPath $build
}
