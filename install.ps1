$ErrorActionPreference = "Stop"

$RepoUrl = "https://github.com/vanphu1201/automatic-promt.git"
$InstallDir = if ($env:STAFFKIT_INSTALL_DIR) { $env:STAFFKIT_INSTALL_DIR } else { Join-Path $HOME ".local\share\staffkit" }

Write-Host "==> Installing StaffKit v3.0.1 into $InstallDir" -ForegroundColor Cyan

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Error "git is required."
}
$PyCmd = if (Get-Command python -ErrorAction SilentlyContinue) { "python" } elseif (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { $null }
if (-not $PyCmd) {
    Write-Error "Python (>=3.11) is required."
}

if (Test-Path (Join-Path $InstallDir ".git")) {
    Write-Host "==> Updating existing repository..."
    git -C $InstallDir pull --ff-only
} else {
    New-Item -ItemType Directory -Force -Path (Split-Path $InstallDir -Parent) | Out-Null
    git clone --depth 1 $RepoUrl $InstallDir
}

$VenvPy = Join-Path $InstallDir ".venv\Scripts\python.exe"
$VenvPip = Join-Path $InstallDir ".venv\Scripts\pip.exe"
if (-not (Test-Path $VenvPy)) {
    Write-Host "==> Creating Python virtualenv..."
    & $PyCmd -m venv (Join-Path $InstallDir ".venv")
}

Write-Host "==> Installing package dependencies..."
& $VenvPip install --quiet -e $InstallDir

Write-Host "==> Registering plugin in ~/.gemini/config/plugins/staffkit..."
& $VenvPy -m engine.cli install-plugin

Write-Host "==> Running 0-token self-verification (plugin-eval)..."
& $VenvPy -m engine.cli plugin-eval

Write-Host ""
Write-Host "DONE! Open Antigravity IDE Plugins Manager to verify StaffKit is Enabled:" -ForegroundColor Green
Write-Host "  /brainstorm-to-product  |  /deep-fix  |  /audit-and-evolve  |  /pr-review  |  /refactor-safe"
