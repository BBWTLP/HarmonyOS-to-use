$ErrorActionPreference='Stop'
$serviceRoot=$PSScriptRoot
foreach($dir in @('.cache','.tmp','.runtime')) {New-Item -ItemType Directory -Force -Path (Join-Path $serviceRoot $dir) | Out-Null}
$env:UV_CACHE_DIR=Join-Path $serviceRoot '.cache/uv'
$env:TEMP=Join-Path $serviceRoot '.tmp'
$env:TMP=$env:TEMP
if(!(Test-Path "$serviceRoot/.venv/Scripts/python.exe")) {
    uv venv "$serviceRoot/.venv" --python 3.11
    if($LASTEXITCODE -ne 0){throw 'venv creation failed'}
}
uv pip install --python "$serviceRoot/.venv/Scripts/python.exe" 'torch==2.10.0' --index-url https://download.pytorch.org/whl/cu128
if($LASTEXITCODE -ne 0){throw 'CUDA PyTorch installation failed'}
$deps=if(Test-Path "$serviceRoot/requirements.lock.txt"){"$serviceRoot/requirements.lock.txt"}else{"$serviceRoot/requirements.in"}
uv pip install --python "$serviceRoot/.venv/Scripts/python.exe" -r $deps
if($LASTEXITCODE -ne 0){throw 'service dependency installation failed'}
& "$serviceRoot/.venv/Scripts/python.exe" "$serviceRoot/download_model.py"
if($LASTEXITCODE -ne 0){throw 'snapshot download or integrity validation failed'}
