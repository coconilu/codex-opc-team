[CmdletBinding()]
param(
    [string]$Python = "python",
    [string]$TargetTriple = "",
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$desktopRoot = Split-Path -Parent $PSScriptRoot
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $desktopRoot "..\.."))
$buildRoot = Join-Path $desktopRoot ".sidecar-build"
$venvRoot = Join-Path $buildRoot "venv"
$pluginRoot = Join-Path $repoRoot "plugins\codex-opc-team"
$entrypoint = Join-Path $pluginRoot "scripts\opc_app.py"
$binaryRoot = Join-Path $desktopRoot "src-tauri\binaries"

if ($Clean -and (Test-Path -LiteralPath $buildRoot)) {
    $resolvedBuild = [System.IO.Path]::GetFullPath($buildRoot)
    $resolvedDesktop = [System.IO.Path]::GetFullPath($desktopRoot)
    if (-not $resolvedBuild.StartsWith($resolvedDesktop, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean a build directory outside the desktop project."
    }
    Remove-Item -LiteralPath $resolvedBuild -Recurse -Force
}

if (-not (Test-Path -LiteralPath $entrypoint -PathType Leaf)) {
    throw "OPC App entrypoint is missing from the repository snapshot."
}

if (-not $TargetTriple) {
    $hostLine = (& rustc -vV | Select-String -Pattern "^host: " | Select-Object -First 1).Line
    if (-not $hostLine) {
        throw "Unable to resolve the Rust target triple."
    }
    $TargetTriple = $hostLine.Substring(6).Trim()
}
if ($TargetTriple -notmatch "^[A-Za-z0-9_.-]+$") {
    throw "Target triple contains unsupported characters."
}

New-Item -ItemType Directory -Force -Path $buildRoot, $binaryRoot | Out-Null
if (-not (Test-Path -LiteralPath (Join-Path $venvRoot "Scripts\python.exe"))) {
    & $Python -m venv $venvRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to create the isolated Python build environment."
    }
}
$venvPython = Join-Path $venvRoot "Scripts\python.exe"
& $venvPython -m pip install --disable-pip-version-check --no-input --requirement (Join-Path $desktopRoot "requirements-build.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Unable to install the pinned sidecar build dependencies."
}

$distRoot = Join-Path $buildRoot "dist"
$workRoot = Join-Path $buildRoot "work"
$specRoot = Join-Path $buildRoot "spec"
$dataSeparator = [System.IO.Path]::PathSeparator
$dataMapping = "$pluginRoot${dataSeparator}plugins\codex-opc-team"

& $venvPython -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --name "opc-sidecar" `
    --distpath $distRoot `
    --workpath $workRoot `
    --specpath $specRoot `
    --paths (Join-Path $pluginRoot "scripts") `
    --add-data $dataMapping `
    $entrypoint
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed to build the OPC sidecar."
}

$extension = if ($IsWindows -or $env:OS -eq "Windows_NT") { ".exe" } else { "" }
$sourceBinary = Join-Path $distRoot "opc-sidecar$extension"
$targetBinary = Join-Path $binaryRoot "opc-sidecar-$TargetTriple$extension"
if (-not (Test-Path -LiteralPath $sourceBinary -PathType Leaf)) {
    throw "PyInstaller did not produce the expected sidecar binary."
}
Copy-Item -LiteralPath $sourceBinary -Destination $targetBinary -Force
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $targetBinary).Hash.ToLowerInvariant()
Write-Output "SIDECAR_PATH=$targetBinary"
Write-Output "SIDECAR_SHA256=$hash"
