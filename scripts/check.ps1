# Show what each server sees: tool count, JDK, Android SDK, lint CLIs.
$Dir = Split-Path -Parent $PSScriptRoot
$Bin = Join-Path $Dir '.venv\Scripts'
if (-not (Test-Path (Join-Path $Bin 'kotlin-mcp.exe'))) {
    Write-Error 'Not installed yet. Run scripts\install.ps1 first.'
    exit 1
}
& (Join-Path $Bin 'files-mcp.exe') --check
Write-Host ''
& (Join-Path $Bin 'kotlin-mcp.exe') --check
