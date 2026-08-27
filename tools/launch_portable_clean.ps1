# Launches the portable Sublime Text test instance (D:\Programs\Sublime Text,
# pointed at the D:\GhostShell-caret-test worktree) with CLAUDE_CODE_* / CLAUDECODE
# env vars stripped from its process environment.
#
# Why this exists: launching the portable instance from inside a Claude Code
# shell (Bash/PowerShell tool call, or any nested terminal a Claude Code session
# spawns) leaks CLAUDE_CODE_CHILD_SESSION and friends down to it via normal
# environment inheritance. The embedded claude.exe session then detects itself
# as a nested/child session and degrades -- disabling transcript saving and,
# confirmed live 2026-08-27, prompt-history recall (Up arrow does nothing).
# That read as a real ai_terminal.py cursor-key bug for an entire investigation
# before the actual cause (environment leakage, not code) was found. See
# ai/TODO.md, "FALSE ALARM, root-caused" entry, same date.
#
# IMPORTANT: the ai_terminal profile this launches is detachable
# (tools/agent_broker.py). If a prior polluted session for this worktree is
# still running, closing Sublime Text alone will NOT clear it -- the orphaned
# broker/claude.exe pair survives independently and a fresh Sublime process
# will silently reattach to it via its recorded named pipe, defeating this
# script's whole point. Run Stop-StalePortableSession first (below) if you
# suspect that's the case, or just check Get-CimInstance Win32_Process for a
# python.exe running agent_broker.py with --cwd D:\GhostShell-caret-test.

param(
    [string]$SublimePath = "D:\Programs\Sublime Text\sublime_text.exe",
    [switch]$KillStaleSession
)

function Stop-StalePortableSession {
    Get-CimInstance Win32_Process |
        Where-Object { $_.CommandLine -match "agent_broker\.py" -and $_.CommandLine -match [regex]::Escape("D:\GhostShell-caret-test") } |
        ForEach-Object {
            $brokerPid = $_.ProcessId
            $child = Get-CimInstance Win32_Process | Where-Object { $_.ParentProcessId -eq $brokerPid }
            if ($child) {
                Write-Output "Killing claude.exe (PID $($child.ProcessId)) under stale broker $brokerPid"
                Stop-Process -Id $child.ProcessId -Force -ErrorAction SilentlyContinue
            }
            Write-Output "Killing stale broker (PID $brokerPid)"
            Stop-Process -Id $brokerPid -Force -ErrorAction SilentlyContinue
        }
}

if ($KillStaleSession) {
    Stop-StalePortableSession
    Start-Sleep -Seconds 1
}

$stripPattern = "^CLAUDE|^AI_AGENT$"

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $SublimePath
$psi.Arguments = "--project none"
$psi.WorkingDirectory = Split-Path $SublimePath
$psi.UseShellExecute = $false

foreach ($e in [System.Environment]::GetEnvironmentVariables().GetEnumerator()) {
    $psi.EnvironmentVariables[$e.Key] = $e.Value
}
$stripKeys = $psi.EnvironmentVariables.Keys | Where-Object { $_ -match $stripPattern }
foreach ($k in $stripKeys) {
    Write-Output "Stripping env var: $k"
    $psi.EnvironmentVariables.Remove($k)
}

[System.Diagnostics.Process]::Start($psi) | Out-Null
Write-Output "Launched $SublimePath with a clean environment."
Write-Output "If the resulting tab still shows 'inherited CLAUDE_CODE_CHILD_SESSION marker' in its footer,"
Write-Output "a stale session reattached -- rerun this script with -KillStaleSession."
