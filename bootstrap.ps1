# Called from the project directory; all project files use relative paths.
function New-ProjectEnvironment {
    $ErrorActionPreference = 'Continue'
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3.13 -m venv .venv
        if ($LASTEXITCODE -eq 0) { return $true }
    }
    foreach ($candidate in @('python', 'python3')) {
        if (Get-Command $candidate -ErrorAction SilentlyContinue) {
            & $candidate -c 'import sys; sys.exit(sys.version_info[:2] != (3, 13))'
            if ($LASTEXITCODE -eq 0) {
                & $candidate -m venv .venv
                if ($LASTEXITCODE -eq 0) { return $true }
            }
        }
    }
    return $false
}

function Initialize-ProjectEnvironment {
    if (Test-Path -LiteralPath '.venv/Scripts/python.exe') { return }
    if (New-ProjectEnvironment) { return }
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw 'Install Python 3.13 from https://www.python.org/downloads/ and run again.'
    }
    & winget install --id Python.Python.3.13 --exact --scope user --silent --accept-source-agreements --accept-package-agreements
    if ($LASTEXITCODE -ne 0) { throw 'Python installation failed.' }
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + $env:Path
    if (-not (New-ProjectEnvironment)) {
        throw 'Restart the terminal after Python installation and run again.'
    }
}
