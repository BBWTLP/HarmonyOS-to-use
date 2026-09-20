$ErrorActionPreference='Stop'
$record=Join-Path $PSScriptRoot '.runtime/process.json'
if(!(Test-Path $record)){Write-Output 'No managed Decider process'; exit 0}
$saved=Get-Content $record -Raw | ConvertFrom-Json
if(!$saved.managed_processes){throw 'Legacy process record; run Start-Decider.ps1 once to refresh process identities'}
$verified=@()
foreach($identity in $saved.managed_processes) {
    $process=Get-CimInstance Win32_Process -Filter "ProcessId=$($identity.pid)" -ErrorAction SilentlyContinue
    if(!$process){continue}
    if($process.CreationDate.ToUniversalTime().Ticks -ne ([datetime]$identity.created).ToUniversalTime().Ticks -or $process.CommandLine -ne $identity.command -or $process.ExecutablePath -ne $identity.executable -or $process.CommandLine -notlike '*-m uvicorn local_service:app*') {throw 'PID identity mismatch; refusing to stop unrelated process'}
    $verified+=$process
}
# Windows venv Python uses a launcher and a child interpreter; stop both verified identities.
[Array]::Reverse($verified)
foreach($process in $verified){Stop-Process -Id $process.ProcessId -ErrorAction SilentlyContinue}
Remove-Item -LiteralPath $record
Write-Output 'Decider stopped (launcher and interpreter)'
