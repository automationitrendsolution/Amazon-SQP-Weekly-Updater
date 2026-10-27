# Registers the weekly SQP update in Windows Task Scheduler (this PC, current user).
#
#   powershell -ExecutionPolicy Bypass -File deploy\install_task.ps1            # install / update
#   powershell -ExecutionPolicy Bypass -File deploy\install_task.ps1 -Remove    # remove
#
# Runs `run.py --weekly --apply --quiet` every Monday at 15:00 (this PC's time, IST): the week that
# ended on Saturday (Sunday-Saturday, decided in US Pacific time, where 15:00 IST Monday is already
# Monday; SQP_AVAILABILITY_LAG_DAYS=2).
# Backup runs Tuesday-Friday at 15:00 only matter when Amazon is slow: Amazon can keep a report in its
# queue for a day or more, and a run waits at most SQP_POLL_TIMEOUT_SECONDS (22 h). A backup run carries
# on with the same reports; once the week is written, it sees that and stops at once.
# Missed runs (PC off) start as soon as the PC is on again. A run still going when the next one is due
# is left alone (no second copy).
# Each run adds one line to logs\cron.log; details are in logs\sqp_updater.log.
param([switch]$Remove)

$TaskName = "Amazon SQP Weekly Updater"
$Project = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Project ".venv\Scripts\pythonw.exe"   # pythonw: no console window

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "Removed '$TaskName'."
    return
}
if (-not (Test-Path $Python)) { throw "Python not found at $Python (create the .venv first)" }

$action = New-ScheduledTaskAction -Execute $Python -Argument "-B run.py --weekly --apply --quiet" -WorkingDirectory $Project
$triggers = @(
    New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 15:00
    New-ScheduledTaskTrigger -Weekly -DaysOfWeek Tuesday, Wednesday, Thursday, Friday -At 15:00
)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable -WakeToRun `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 23) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings `
    -Description "Amazon Brand Analytics SQP (SP-API) -> powerbidb.2024BrandAnalytics: last week, every Monday 15:00 (Tue-Fri: backup runs while Amazon is slow)" -Force | Out-Null
Write-Output "Registered '$TaskName': Monday 15:00 (backup Tue-Fri 15:00), $Python -B run.py --weekly --apply --quiet (in $Project)"
