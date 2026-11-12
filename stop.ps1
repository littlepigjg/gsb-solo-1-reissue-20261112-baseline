$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pidFile = '.\logs\server.pid'
if (-not (Test-Path -LiteralPath $pidFile)) {
    Write-Host 'No background server PID was recorded. For a terminal server, use Ctrl+C in its window.'
    exit 0
}
$studioProcessId = [int](Get-Content -LiteralPath $pidFile -Raw).Trim()
$expectedPython = '.venv\Scripts\python.exe'
$studioProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $studioProcessId"
if ($studioProcess -and ((Resolve-Path -Relative -LiteralPath $studioProcess.ExecutablePath) -replace '^\.\\', '') -ieq $expectedPython -and $studioProcess.CommandLine -match 'run\.py') {
    & taskkill.exe /PID $studioProcessId /T /F
    if ($LASTEXITCODE -ne 0) { throw 'Could not stop the recorded process tree.' }
    Remove-Item -LiteralPath $pidFile
    Write-Host 'Image Studio background server stopped.'
} else {
    Write-Host 'Recorded PID is no longer the Image Studio process. No process was stopped.'
}
