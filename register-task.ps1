<#
  Zaregistruje do Planovace uloh Windows dennu ulohu (Po-Pa 5:43),
  ktera spusti sber dat o zpozdeni prestupu 336 -> 384 na Zlicine.

  Spusteni (v PowerShellu, staci bezne prava uzivatele):
      powershell -ExecutionPolicy Bypass -File .\register-task.ps1

  Odregistrovani:
      Unregister-ScheduledTask -TaskName "PID transfer monitor" -Confirm:$false
#>

$ErrorActionPreference = "Stop"
$here    = Split-Path -Parent $MyInvocation.MyCommand.Path
$python  = (Get-Command python.exe).Source
$script  = Join-Path $here "monitor.py"
$taskName = "PID transfer monitor"

if (-not (Test-Path (Join-Path $here "config.json"))) {
    Write-Warning "Chybi config.json - zkopiruj config.example.json na config.json a doplH API klic."
}

$action = New-ScheduledTaskAction -Execute $python -Argument "`"$script`" collect" -WorkingDirectory $here

# Po-Pa v 5:43 rano
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 5:43am

$settings = New-ScheduledTaskSettingsSet `
    -WakeToRun `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description "Sber realtime zpozdeni PID pro prestup 336->384 na Zlicine. Zdroj: monitor.py"

Write-Host ""
Write-Host "Uloha '$taskName' zaregistrovana." -ForegroundColor Green
Write-Host "Python : $python"
Write-Host "Skript : $script"
Write-Host ""
Write-Host "Test hned ted:  Start-ScheduledTask -TaskName '$taskName'"
Write-Host "Stav        :  Get-ScheduledTaskInfo -TaskName '$taskName'"
Write-Host ""
Write-Host "POZOR: 'Probudit pocitac' funguje jen ze spanku (S3), ne z hibernace"
Write-Host "ani z vypnuteho stavu. Kdyz PC v 5:43 spi a nevzbudi se, ten den se"
Write-Host "proste vynecha (v datech nebude zadny radek)."
