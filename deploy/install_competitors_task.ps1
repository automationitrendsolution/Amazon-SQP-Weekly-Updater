# Registers the nightly Search Query Details (competitor) scraping in Windows Task Scheduler (this PC, current user).
#
#   powershell -ExecutionPolicy Bypass -File deploy\install_competitors_task.ps1            # install / update
#   powershell -ExecutionPolicy Bypass -File deploy\install_competitors_task.ps1 -Remove    # remove
#
# Runs `run.py --sc-night --quiet` every day at 00:00, 03:00 and 06:00 (this PC's time, IST = US Pacific business
# hours). Each part opens up to 400 Search Query Details pages of the oldest week not fully saved (from
# SC_COMPETITORS_FROM), then loads that week's saved pages into powerbidb.BrandAnalytics_Competitor (no duplicates).
# A part stops by itself on a captcha / warning / "Access Required" page, or when the saved Seller Central
# sign-in has expired (it never logs in, so no OTP is sent): run `run.py --sc-login` once, the next part continues.
# Needs the PC on and the user signed in to Windows (screen may be locked): Chrome opens a window.
# A part still running when the next is due is left alone; missed parts (PC off) are not started later in the day.
param([switch]$Remove)

$TaskName = "Amazon SQP Competitors"
$Project = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Project ".venv\Scripts\pythonw.exe"   # pythonw: no console window

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "Removed '$TaskName'."
    return
}
if (-not (Test-Path $Python)) { throw "Python not found at $Python (create the .venv first)" }

$action = New-ScheduledTaskAction -Execute $Python -Argument "-B run.py --sc-night --quiet" -WorkingDirectory $Project
$triggers = @(
    New-ScheduledTaskTrigger -Daily -At 00:00
    New-ScheduledTaskTrigger -Daily -At 03:00
    New-ScheduledTaskTrigger -Daily -At 06:00
)
$settings = New-ScheduledTaskSettingsSet -RunOnlyIfNetworkAvailable -WakeToRun -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3 -Minutes 15) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings `
    -Description "Brand Analytics Search Query Details (Seller Central) -> powerbidb.BrandAnalytics_Competitor: 3 parts a night, 400 pages each" -Force | Out-Null
Write-Output "Registered '$TaskName': daily 00:00, 03:00, 06:00, $Python -B run.py --sc-night --quiet (in $Project)"
