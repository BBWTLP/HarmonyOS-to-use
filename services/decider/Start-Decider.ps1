param([int]$Port=8765)
$ErrorActionPreference='Stop'
$serviceRoot=$PSScriptRoot
$runtimeDir=Join-Path $serviceRoot '.runtime'
New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
function Save-ManagedProcesses($Launcher, [int]$ServicePort) {
    $entries=@($Launcher)
    for($attempt=0;$attempt -lt 20;$attempt++) {
        $children=@(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($Launcher.ProcessId)" | Where-Object {$_.CommandLine -like '*-m uvicorn local_service:app*'})
        if($children.Count -gt 0){$entries+=$children;break}
        Start-Sleep -Milliseconds 100
    }
    $managed=@($entries | ForEach-Object {@{pid=$_.ProcessId;created=$_.CreationDate.ToUniversalTime().ToString('o');executable=$_.ExecutablePath;command=$_.CommandLine}})
    @{pid=$Launcher.ProcessId;created=$Launcher.CreationDate.ToUniversalTime().ToString('o');port=$ServicePort;root=$serviceRoot;managed_processes=$managed} | ConvertTo-Json -Depth 4 | Set-Content "$runtimeDir/process.json" -Encoding UTF8
}
if (Test-Path "$runtimeDir/process.json") {
    $saved=Get-Content "$runtimeDir/process.json" -Raw | ConvertFrom-Json
    $running=Get-CimInstance Win32_Process -Filter "ProcessId=$($saved.pid)" -ErrorAction SilentlyContinue
    if ($running -and $running.CreationDate.ToUniversalTime().Ticks -eq ([datetime]$saved.created).ToUniversalTime().Ticks -and $running.CommandLine -like '*local_service:app*') {
        Save-ManagedProcesses $running $saved.port
        Write-Output "Decider already running: http://127.0.0.1:$($saved.port)"
        exit 0
    }
}
if(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {throw "Port $Port is already occupied"}
if(!(Test-Path "$runtimeDir/api-token")) {
    $bytes=New-Object byte[] 32
    $rng=[System.Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes); $rng.Dispose()
    [IO.File]::WriteAllText("$runtimeDir/api-token",[Convert]::ToBase64String($bytes))
}
$env:HF_HUB_OFFLINE='1'
$env:TRANSFORMERS_OFFLINE='1'
$env:HF_HUB_DISABLE_TELEMETRY='1'
$env:DO_NOT_TRACK='1'
$env:HF_HOME=Join-Path $serviceRoot '.cache/huggingface'
$env:TEMP=Join-Path $serviceRoot '.tmp'
$env:TMP=$env:TEMP
$env:DECIDER_MODEL_REVISION='7789eb65d5cf519737608e218fa88819bddea0af'
$process=Start-Process -FilePath "$serviceRoot/.venv/Scripts/python.exe" -WorkingDirectory $serviceRoot -ArgumentList @('-m','uvicorn','local_service:app','--host','127.0.0.1','--port',"$Port",'--workers','1','--no-access-log','--timeout-keep-alive','5') -WindowStyle Hidden -RedirectStandardOutput "$runtimeDir/stdout.log" -RedirectStandardError "$runtimeDir/stderr.log" -PassThru
$info=Get-CimInstance Win32_Process -Filter "ProcessId=$($process.Id)"
Save-ManagedProcesses $info $Port
Write-Output "Decider starting with PID $($process.Id) at http://127.0.0.1:$Port; run Test-Decider.ps1 for readiness."
