# Pre-release gate. Must exit 0 before any version bump, desktop-v* tag, or publish.
param(
    [switch]$SkipRust
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Step([string]$Name, [scriptblock]$Action) {
    Write-Host ""
    Write-Host "==> $Name"
    & $Action
    if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) {
        throw "FAILED: $Name (exit $LASTEXITCODE)"
    }
    Write-Host "OK  $Name"
}

$python = if (Test-Path ".\api\.venv\Scripts\python.exe") {
    (Resolve-Path ".\api\.venv\Scripts\python.exe").Path
} else {
    "python"
}

Step "version files match" {
    $packageVersion = (Get-Content package.json | ConvertFrom-Json).version
    $tauriVersion = (Get-Content src-tauri/tauri.conf.json | ConvertFrom-Json).version
    $cargoVersion = (Select-String -Path src-tauri/Cargo.toml -Pattern '^version = "([^"]+)"').Matches[0].Groups[1].Value
    if ($packageVersion -ne $tauriVersion -or $packageVersion -ne $cargoVersion) {
        throw "package.json=$packageVersion tauri.conf.json=$tauriVersion Cargo.toml=$cargoVersion"
    }
    Write-Host "version $packageVersion"
}

Step "javascript syntax" {
    node --check desktop-ui/app.js
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    node --check desktop-ui/app.test.js
}

Step "ui copy and source contracts" {
    node scripts/verify-release-ui.mjs
}

Step "backend tests" {
    $env:PYTHONPATH = $root
    & $python -m pytest tests/backend -q --tb=short
}

Step "windows clipboard roundtrip" {
    $marker = "CODEINSIGHT-RELEASE-VERIFY-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
    Set-Clipboard -Value $marker
    $readBack = (Get-Clipboard -Raw).Trim()
    if ($readBack -ne $marker) {
        throw "clipboard roundtrip failed"
    }
}

if ($SkipRust) {
    Write-Host ""
    Write-Host "SKIP rust host checks (-SkipRust)"
} else {
    $cargoHome = Join-Path $env:USERPROFILE ".cargo\bin"
    if (Test-Path $cargoHome) {
        $env:Path = "$cargoHome;" + $env:Path
    }
    if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
        throw "cargo is required for a desktop release. Install Rust, then re-run npm run verify:release."
    }
    Step "cargo fmt" {
        cargo fmt --manifest-path src-tauri/Cargo.toml --check
    }
    Step "cargo test" {
        New-Item -ItemType Directory -Force src-tauri/binaries | Out-Null
        $sidecar = "src-tauri/binaries/codeinsight-daemon-x86_64-pc-windows-msvc.exe"
        if (-not (Test-Path $sidecar)) {
            New-Item -ItemType File -Force -Path $sidecar | Out-Null
        }
        cargo test --release --manifest-path src-tauri/Cargo.toml
    }
}

Write-Host ""
Write-Host "verify-release: all required checks passed"
