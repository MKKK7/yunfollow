param([Parameter(Mandatory=$true)][string]$ConfigPath)
$ErrorActionPreference = 'Stop'
$configFile = (Resolve-Path -LiteralPath $ConfigPath).Path
$nodeExe = (Get-Command node -ErrorAction Stop).Source
$bridgeFile = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../fnos-adapters/kdocs-bridge.mjs')).Path
$runnerFile = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot 'run-document-bridge.ps1')).Path
$taskName = 'Reelay-FNOS-Documents'
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing -and $existing.Actions.Arguments -notlike '*run-document-bridge.ps1*') {
    throw 'An unrelated scheduled task already uses the target name.'
}
$arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -File "' + $runnerFile + '" -NodePath "' + $nodeExe + '" -BridgePath "' + $bridgeFile + '" -ConfigPath "' + $configFile + '"'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 15)
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -Hidden
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Output 'Document bridge scheduled every15minutes for the currently logged-in user.'
