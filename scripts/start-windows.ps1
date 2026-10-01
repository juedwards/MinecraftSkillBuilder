# Start Minecraft Skill Builder on Windows (no WSL needed).
# First run: installs uv (which brings its own Python) and the app's dependencies.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv (Python package manager) for this user..."
    powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

# A separate environment folder, so a WSL checkout of the same folder keeps working too.
$env:UV_PROJECT_ENVIRONMENT = ".venv-windows"
# Trust the certificates Windows trusts (school and company networks often inspect HTTPS).
$env:UV_SYSTEM_CERTS = "1"
# Minecraft runs on this PC, so only listen locally (no firewall prompt, not reachable from the network).
uv run --frozen skillbuilder --open --host 127.0.0.1 @args
