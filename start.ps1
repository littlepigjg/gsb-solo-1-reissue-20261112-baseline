param([int]$Port = 8765)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$venvPython = '.\.venv\Scripts\python.exe'
. .\bootstrap.ps1
Initialize-ProjectEnvironment
& $venvPython -c 'import flask, PIL, numpy, waitress' 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Installing project dependencies...'
    & $venvPython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check the network and try again.' }
}
$url = "http://127.0.0.1:$Port"
try {
    $health = Invoke-RestMethod -Uri "$url/api/health" -TimeoutSec 2
    if ($health.ok -and $health.data.storage -eq 'JSON' -and $health.data.features -eq 10) {
        Write-Host "Image Studio is already running: $url"
        Start-Process $url
        exit 0
    }
} catch { }
Write-Host "Open $url in your browser. Keep this window open while using the app."
& $venvPython run.py --port $Port
