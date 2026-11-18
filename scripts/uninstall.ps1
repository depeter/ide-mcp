# Unregister both servers from Codex and Claude Code. Pass -Purge to also delete .venv.
#
#   scripts\uninstall.ps1 [-Purge]
[CmdletBinding()]
param([switch]$Purge)

$Dir = Split-Path -Parent $PSScriptRoot

function Find-App([string]$Name) {
    $cmd = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) { return $cmd.Source }
    return $null
}

function Remove-Server([string]$Cli, [string[]]$Arguments) {
    $ErrorActionPreference = 'SilentlyContinue'
    & $Cli @Arguments *> $null
    return ($LASTEXITCODE -eq 0)
}

$codex = Find-App 'codex'
$claudeCli = Find-App 'claude'
foreach ($s in @('files', 'kotlin')) {
    if ($codex -and (Remove-Server $codex @('mcp', 'remove', $s))) { Write-Host "Removed '$s' from Codex." }
    if ($claudeCli -and (Remove-Server $claudeCli @('mcp', 'remove', '--scope', 'user', $s))) { Write-Host "Removed '$s' from Claude Code." }
}
if ($Purge) {
    $venv = Join-Path $Dir '.venv'
    if (Test-Path $venv) {
        # A server still running out of the virtualenv keeps its files locked on Windows.
        $prefix = $venv + '\'
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
            $_.ProcessId -ne $PID -and $_.ExecutablePath -and
            $_.ExecutablePath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
        } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Remove-Item -Recurse -Force $venv
        Write-Host "Deleted $venv"
    }
}
