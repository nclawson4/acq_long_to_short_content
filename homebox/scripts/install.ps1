# Home-box one-shot setup.
#
# Idempotent. Re-run after pulling new code to refresh deps + restart the task.
#
# Requires (run elsewhere first):
#   - Admin PowerShell
#   - Node + the binaries (yt-dlp.exe, ffmpeg.exe, ffprobe.exe) already
#     placed in C:\acq-ingest by the bootstrap step.
#   - .env file at C:\acq-ingest\.env with BLOB_READ_WRITE_TOKEN and
#     ACQ_INGEST_SECRET.

$ErrorActionPreference = "Stop"
$base = "C:\acq-ingest"
$taskName = "AcqHomeboxIngest"

Write-Host "==> Installing npm deps..."
Push-Location $base
& "C:\Program Files\nodejs\npm.cmd" install --no-audit --no-fund --omit=dev
Pop-Location

Write-Host "==> Loading .env into the scheduled task environment..."
$envPath = Join-Path $base ".env"
if (!(Test-Path $envPath)) { throw ".env not found at $envPath" }
$envLines = Get-Content $envPath | Where-Object { $_ -and ($_ -notmatch '^\s*#') }
$envBlock = ($envLines -join "`r`n")

Write-Host "==> Registering scheduled task '$taskName' (run at startup, as current user)..."
$action = New-ScheduledTaskAction `
  -Execute "C:\Program Files\nodejs\node.exe" `
  -Argument "$base\src\server.js" `
  -WorkingDirectory $base

$trigger = New-ScheduledTaskTrigger -AtStartup
# On standalone Windows the principal needs COMPUTERNAME\USERNAME, not
# USERDOMAIN\USERNAME — USERDOMAIN resolves to "WORKGROUP" which the task
# scheduler rejects with "no mapping between account names and security IDs".
$principal = New-ScheduledTaskPrincipal -UserId "$env:COMPUTERNAME\$env:USERNAME" -LogonType S4U -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 0) -RestartInterval (New-TimeSpan -Minutes 1) -RestartCount 999

if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
  Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null

# Env vars for the task can't be set inline on Win10's task scheduler — we
# read them from .env inside the server, so writing them to a small wrapper
# is unnecessary. Instead just dot-source .env into the user's persistent env
# so node sees them when the task starts.
foreach ($line in $envLines) {
  $k, $v = $line -split '=', 2
  if ($k -and $v) {
    [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), [EnvironmentVariableTarget]::User)
  }
}

Write-Host "==> Starting the task now..."
Start-ScheduledTask -TaskName $taskName

Start-Sleep -Seconds 3
Write-Host "==> Health check..."
try {
  $r = Invoke-WebRequest -Uri "http://127.0.0.1:8787/health" -UseBasicParsing -TimeoutSec 5
  Write-Host "Health: $($r.Content)"
} catch {
  Write-Host "Health check failed: $($_.Exception.Message)"
  Write-Host "Tail of $base\server.log:"
  if (Test-Path "$base\server.log") { Get-Content "$base\server.log" -Tail 20 }
}
