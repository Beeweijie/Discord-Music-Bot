param(
    [switch]$SkipPythonInstall,
    [switch]$SkipFfmpegInstall,
    [switch]$InstallStartup
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot

function Test-Command {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Install-WithWinget {
    param(
        [string]$PackageId,
        [string]$Name
    )

    if (-not (Test-Command "winget")) {
        Write-Warning "winget is not available. Please install $Name manually."
        return $false
    }

    Write-Host "Installing $Name with winget..."
    winget install --id $PackageId --exact --silent --accept-package-agreements --accept-source-agreements
    return ($LASTEXITCODE -eq 0)
}

function Find-Python {
    $Candidates = @(
        ".\.venv\Scripts\python.exe",
        "$env:LocalAppData\Programs\Python\Python314\python.exe",
        "$env:LocalAppData\Programs\Python\Python313\python.exe",
        "$env:LocalAppData\Programs\Python\Python312\python.exe"
    )

    foreach ($Candidate in $Candidates) {
        if (Test-Path $Candidate) {
            return (Resolve-Path $Candidate).Path
        }
    }

    if (Test-Command "py") {
        return "py"
    }

    if (Test-Command "python") {
        return "python"
    }

    return $null
}

if (-not $SkipPythonInstall) {
    $Python = Find-Python
    if (-not $Python) {
        $Installed = Install-WithWinget -PackageId "Python.Python.3.14" -Name "Python 3.14"
        if (-not $Installed) {
            Write-Warning "Python 3.14 install failed or is unavailable. Trying Python 3.13."
            Install-WithWinget -PackageId "Python.Python.3.13" -Name "Python 3.13" | Out-Null
        }
    }
}

if (-not $SkipFfmpegInstall) {
    if (-not (Test-Command "ffmpeg") -and -not (Test-Path "C:\Program Files\ffmpeg\bin\ffmpeg.exe")) {
        Install-WithWinget -PackageId "Gyan.FFmpeg" -Name "FFmpeg" | Out-Null
    }
}

$Python = Find-Python
if (-not $Python) {
    throw "Python was not found. Install Python and rerun this script."
}

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    Write-Host "Creating virtual environment..."
    if ($Python -eq "py") {
        py -3 -m venv .venv
    } else {
        & $Python -m venv .venv
    }
}

$VenvPython = Resolve-Path ".\.venv\Scripts\python.exe"
Write-Host "Upgrading pip..."
& $VenvPython -m pip install --upgrade pip

Write-Host "Installing Python dependencies..."
& $VenvPython -m pip install -r requirements.txt

if (-not (Test-Path ".\.env")) {
    Copy-Item ".\.env.example" ".\.env" -ErrorAction SilentlyContinue
    if (-not (Test-Path ".\.env")) {
        "DISCORD_TOKEN=your_token_here" | Out-File -Encoding utf8 ".\.env"
    }
    Write-Warning "Created .env. Edit DISCORD_TOKEN before starting the bot."
}

if ($InstallStartup) {
    & (Join-Path $PSScriptRoot "install_startup.ps1")
}

Write-Host ""
Write-Host "Setup complete."
Write-Host "Edit .env, then run: scripts\start_bot.bat"
