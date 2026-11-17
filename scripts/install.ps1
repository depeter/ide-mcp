# Build the virtualenv and register both servers with Codex (and optionally Claude Code).
#
#   scripts\install.ps1                build + register "files" and "kotlin" with Codex
#   scripts\install.ps1 -Claude        also register with Claude Code (user scope)
#   scripts\install.ps1 -NoCodex       build only (combine with -Claude if wanted)
#   scripts\install.ps1 -Only kotlin   register just one of the two servers (files | kotlin)
#   scripts\install.ps1 -WithLsp       also download JetBrains' Kotlin language server (370 MB) for
#                                      code-aware find usages, rename and IntelliJ inspections
#
# If script execution is blocked, run it as:
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#
# Safe to re-run: it replaces any existing registration of the same names.
[CmdletBinding()]
param(
    [switch]$Claude,
    [switch]$NoCodex,
    [switch]$WithLsp,
    [ValidateSet('files', 'kotlin')][string]$Only
)

$ErrorActionPreference = 'Stop'
$Dir = Split-Path -Parent $PSScriptRoot
$Bin = Join-Path $Dir '.venv\Scripts'
$KotlinTimeout = 1800
$Servers = @('files', 'kotlin')
if ($Only) { $Servers = @($Only) }

# Resolve a real executable (.exe/.cmd), never a .ps1 shim: npm's PowerShell shims swallow the
# "--" separator that `mcp add` needs.
function Find-App([string]$Name) {
    $cmd = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) { return $cmd.Source }
    return $null
}

# Run a native command, discarding its output and ignoring failure.
function Invoke-Quiet([string]$Exe, [string[]]$Arguments) {
    $ErrorActionPreference = 'SilentlyContinue'
    & $Exe @Arguments *> $null
}

function Test-Python([string]$Exe, [string[]]$Prefix) {
    # The Microsoft Store "python" alias exists on PATH but only prints an install hint.
    $ErrorActionPreference = 'SilentlyContinue'
    & $Exe @Prefix -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' *> $null
    return ($LASTEXITCODE -eq 0)
}

Write-Host "==> Building virtualenv in $Dir\.venv"
$uv = Find-App 'uv'
if ($uv) {
    Push-Location $Dir
    try { & $uv sync --quiet --no-dev } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed (exit $LASTEXITCODE)." }
}
else {
    $py = $null; $pyArgs = @()
    $launcher = Find-App 'py'
    $python = Find-App 'python'
    if ($launcher -and (Test-Python $launcher @('-3'))) { $py = $launcher; $pyArgs = @('-3') }
    elseif ($python -and (Test-Python $python @())) { $py = $python }
    if (-not $py) { throw 'Need either uv (https://docs.astral.sh/uv/) or Python 3.10+ on PATH.' }
    & $py @pyArgs -m venv (Join-Path $Dir '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the virtualenv.' }
    & (Join-Path $Bin 'python.exe') -m pip install --quiet --upgrade pip
    & (Join-Path $Bin 'python.exe') -m pip install --quiet -e $Dir
    if ($LASTEXITCODE -ne 0) { throw 'pip install failed.' }
}

foreach ($s in $Servers) {
    $exe = Join-Path $Bin "$s-mcp.exe"
    if (-not (Test-Path $exe)) { throw "Build did not produce $exe; not registering anything." }
}

# Agents start MCP servers with a trimmed environment, so pin what Gradle needs.
function Test-Jdk([string]$Path) {
    return ($Path -and (Test-Path (Join-Path $Path 'bin\java.exe')))
}
$Java = $null
if (Test-Jdk $env:JAVA_HOME) { $Java = $env:JAVA_HOME }
if (-not $Java) {
    $javaExe = Find-App 'java'
    if ($javaExe) {
        $candidate = Split-Path -Parent (Split-Path -Parent $javaExe)
        # Oracle's "javapath" shim directory is on PATH but is not a JDK home.
        if ((Test-Jdk $candidate) -and (Test-Path (Join-Path $candidate 'release'))) { $Java = $candidate }
    }
}
if (-not $Java) {
    # Android Studio ships a JDK; use it when nothing else is installed.
    $studio = @(
        (Join-Path $env:ProgramFiles 'Android\Android Studio\jbr'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Android Studio\jbr')
    )
    foreach ($c in $studio) { if (Test-Jdk $c) { $Java = $c; break } }
}
$Sdk = $env:ANDROID_HOME
if (-not $Sdk) { $Sdk = $env:ANDROID_SDK_ROOT }
if (-not $Sdk) {
    $c = Join-Path $env:LOCALAPPDATA 'Android\Sdk'
    if (Test-Path $c) { $Sdk = $c }
}
$KotlinEnv = @()
if ($Java) { $KotlinEnv += @('--env', "JAVA_HOME=$Java") }
else { Write-Warning 'No JDK found; Gradle tools will fail until JAVA_HOME is set. Set it and re-run.' }
if ($Sdk) { $KotlinEnv += @('--env', "ANDROID_HOME=$Sdk") }

if ($WithLsp) {
    Write-Host '==> Installing the Kotlin language server'
    & (Join-Path $Bin 'kotlin-mcp.exe') --install-lsp
    if ($LASTEXITCODE -ne 0) { Write-Warning 'Language server install failed; the text-based tools still work.' }
}

Write-Host '==> Self-check'
$savedJava = $env:JAVA_HOME; $savedSdk = $env:ANDROID_HOME
if ($Java) { $env:JAVA_HOME = $Java }
if ($Sdk) { $env:ANDROID_HOME = $Sdk }
foreach ($s in $Servers) { & (Join-Path $Bin "$s-mcp.exe") --check }
$env:JAVA_HOME = $savedJava; $env:ANDROID_HOME = $savedSdk

function Register-Servers([string]$Cli, [string[]]$ScopeArgs) {
    foreach ($s in $Servers) {
        $envArgs = @()
        if ($s -eq 'kotlin') { $envArgs = $KotlinEnv }
        Invoke-Quiet $Cli (@('mcp', 'remove') + $ScopeArgs + @($s))
        $addArgs = @('mcp', 'add') + $ScopeArgs + @($s) + $envArgs + @('--', (Join-Path $Bin "$s-mcp.exe"))
        & $Cli @addArgs
        if ($LASTEXITCODE -ne 0) { Write-Warning "Registering '$s' failed (exit $LASTEXITCODE)." }
    }
}

if (-not $NoCodex) {
    $codex = Find-App 'codex'
    if ($codex) {
        Write-Host "==> Registering $($Servers -join ', ') with Codex"
        Register-Servers $codex @()
        # Codex kills tool calls after 60s by default, which no real Gradle build survives.
        $codexHome = $env:CODEX_HOME
        if (-not $codexHome) { $codexHome = Join-Path $HOME '.codex' }
        $config = Join-Path $codexHome 'config.toml'
        if (($Servers -contains 'kotlin') -and (Test-Path $config)) {
            $text = [IO.File]::ReadAllText($config)
            $header = [regex]::Match($text, '(?m)^\[mcp_servers\.kotlin\][ \t]*\r?\n')
            if ($header.Success) {
                $start = $header.Index + $header.Length
                $next = [regex]::Match($text.Substring($start), '(?m)^\[(?!mcp_servers\.kotlin[.\]])')
                $section = $text.Substring($start)
                if ($next.Success) { $section = $text.Substring($start, $next.Index) }
                if ($section -notmatch 'tool_timeout_sec') {
                    $text = $text.Insert($start, "tool_timeout_sec = $KotlinTimeout`n")
                    [IO.File]::WriteAllText($config, $text, (New-Object Text.UTF8Encoding $false))
                    Write-Host "    set tool_timeout_sec = $KotlinTimeout for 'kotlin' in $config"
                }
            }
            else {
                Write-Warning "[mcp_servers.kotlin] not found in $config; add tool_timeout_sec = $KotlinTimeout to it yourself."
            }
        }
    }
    else {
        Write-Host '==> codex CLI not found. Add this to ~\.codex\config.toml yourself:'
        foreach ($s in $Servers) {
            Write-Host ''
            Write-Host "[mcp_servers.$s]"
            Write-Host "command = '$(Join-Path $Bin "$s-mcp.exe")'"
            if ($s -eq 'kotlin') {
                Write-Host "tool_timeout_sec = $KotlinTimeout"
                Write-Host "env = { JAVA_HOME = '$Java', ANDROID_HOME = '$Sdk' }"
            }
        }
        Write-Host ''
    }
}

if ($Claude) {
    $claudeCli = Find-App 'claude'
    if ($claudeCli) {
        Write-Host "==> Registering $($Servers -join ', ') with Claude Code (user scope)"
        Register-Servers $claudeCli @('--scope', 'user')
    }
    else { Write-Warning 'claude CLI not found; skipped.' }
}

Write-Host ''
Write-Host "Done. Start a new agent session to pick up: $($Servers -join ', ')"
