param([int]$Port=8765)
$ErrorActionPreference='Stop'
Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 5 | ConvertTo-Json
