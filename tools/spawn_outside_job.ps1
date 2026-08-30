# Create a broker through the Task Scheduler service, outside Sublime's
# kill-on-close job. The task exists only long enough to start the process;
# the broker's complete configuration and environment live in its randomized
# one-use launch file, not in this helper's command line.
$ErrorActionPreference = 'Stop'

$taskName = $null
try {
    $payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $taskName = [string]$payload.task_name
    $action = New-ScheduledTaskAction `
        -Execute ([string]$payload.executable) `
        -Argument ([string]$payload.arguments) `
        -WorkingDirectory ([string]$payload.cwd)
    $principal = New-ScheduledTaskPrincipal `
        -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
        -LogonType Interactive `
        -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero)
    $task = New-ScheduledTask `
        -Action $action `
        -Principal $principal `
        -Settings $settings
    Register-ScheduledTask `
        -TaskName $taskName `
        -InputObject $task | Out-Null
    Start-ScheduledTask -TaskName $taskName

    # The broker deletes the launch file as its first action. Waiting for that
    # handshake proves the scheduled action started before its registration is
    # removed. Unregistering does not send Stop to the already-running action.
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    $launchPath = [string]$payload.launch_file
    while ((Test-Path -LiteralPath $launchPath) -and
           [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 50
    }
    if (Test-Path -LiteralPath $launchPath) {
        throw 'scheduled broker did not consume its launch file'
    }
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    $taskName = $null
    @{ return_value = 0; process_id = 0 } |
        ConvertTo-Json -Compress
} catch {
    if ($taskName) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false `
            -ErrorAction SilentlyContinue
    }
    [Console]::Error.WriteLine($_.Exception.ToString())
    exit 1
}
