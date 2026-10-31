param(
    [Parameter(Mandatory=$true)][string]$NodePath,
    [Parameter(Mandatory=$true)][string]$BridgePath,
    [Parameter(Mandatory=$true)][string]$ConfigPath
)
$ErrorActionPreference = 'Stop'
$privateFolder = Split-Path -Parent $ConfigPath
$process = Start-Process -FilePath $NodePath -ArgumentList @('"' + $BridgePath + '"', '"' + $ConfigPath + '"') -WindowStyle Hidden -PassThru -Wait -RedirectStandardOutput (Join-Path $privateFolder 'bridge-last.log') -RedirectStandardError (Join-Path $privateFolder 'bridge-last-error.log')
exit $process.ExitCode
