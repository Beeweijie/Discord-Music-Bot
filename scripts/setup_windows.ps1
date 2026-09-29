param(
    [switch]$SkipPythonInstall,
    [switch]$SkipFfmpegInstall,
    [switch]$InstallStartup,
    [switch]$NoConfigure
)
$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location -LiteralPath $ProjectRoot

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
        [Environment]::GetEnvironmentVariable("Path", "User") + ";" + $env:Path
}
function Invoke-Checked {
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Executable failed (exit code $LASTEXITCODE). Fix the error above and run install.bat again."
    }
}
function Install-WithWinget {
    param([string]$PackageId, [string]$Name)
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "Install $Name manually, or install App Installer from Microsoft Store to enable winget, then retry."
    }
    Invoke-Checked "winget" @("install", "--id", $PackageId, "--exact", "--silent",
        "--accept-package-agreements", "--accept-source-agreements")
    Refresh-Path
}
function Find-Python {
    $Candidates = @(
        (Join-Path $ProjectRoot ".venv\Scripts\python.exe"),
        "$env:LocalAppData\Programs\Python\Python313\python.exe",
        "$env:LocalAppData\Programs\Python\Python312\python.exe",
        "$env:LocalAppData\Programs\Python\Python314\python.exe"
    )
    foreach ($Command in @("py", "python", "python3")) {
        $Found = Get-Command $Command -ErrorAction SilentlyContinue
        if ($Found) { $Candidates += $Found.Source }
    }
    foreach ($Candidate in $Candidates) {
        if (-not (Test-Path -LiteralPath $Candidate)) { continue }
        try {
            & $Candidate -c "import sys, venv; sys.exit(0 if (3, 12) <= sys.version_info[:2] < (3, 15) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) { return $Candidate }
        } catch { }
    }
    return $null
}
function Find-Ffmpeg {
    $Command = Get-Command ffmpeg -ErrorAction SilentlyContinue
    $Candidates = @("C:\Program Files\ffmpeg\bin\ffmpeg.exe")
    if ($Command) { $Candidates = @($Command.Source) + $Candidates }
    $Packages = Join-Path $env:LocalAppData "Microsoft\WinGet\Packages"
    if (Test-Path -LiteralPath $Packages) {
        $Candidates += @(Get-ChildItem -LiteralPath $Packages -Filter ffmpeg.exe -File -Recurse |
            Select-Object -ExpandProperty FullName)
    }
    foreach ($Candidate in $Candidates) {
        if (-not (Test-Path -LiteralPath $Candidate)) { continue }
        & $Candidate -version *> $null
        if ($LASTEXITCODE -eq 0) { return $Candidate }
    }
    return $null
}
try {
    Refresh-Path
    $Python = Find-Python
    if (-not $Python -and -not $SkipPythonInstall) {
        Install-WithWinget "Python.Python.3.13" "Python 3.13"
        $Python = Find-Python
    }
    if (-not $Python) { throw "Python 3.12-3.14 was not found. Install Python 3.13 and retry." }
    $Ffmpeg = Find-Ffmpeg
    if (-not $Ffmpeg -and -not $SkipFfmpegInstall) {
        Install-WithWinget "Gyan.FFmpeg" "FFmpeg"
        $Ffmpeg = Find-Ffmpeg
    }
    if (-not $Ffmpeg) { throw "FFmpeg was not found. Install FFmpeg, add it to PATH and retry." }
    $VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if ($Python -ne $VenvPython) {
        Write-Host "Creating virtual environment..."
        Invoke-Checked $Python @("-m", "venv", ".venv")
    }
    Invoke-Checked $VenvPython @("-m", "pip", "install", "--upgrade", "pip")
    Invoke-Checked $VenvPython @("-m", "pip", "install", "-r", "requirements.txt")
    $EnvFile = Join-Path $ProjectRoot ".env"
    if (-not (Test-Path -LiteralPath $EnvFile)) {
        Copy-Item -LiteralPath (Join-Path $ProjectRoot ".env.example") -Destination $EnvFile
    }
    # Persist the path for future tray launches; preserve existing credentials.
    $Text = [IO.File]::ReadAllText($EnvFile)
    if ($Text -notmatch '(?m)^\s*FFMPEG_PATH\s*=\s*\S+') {
        $Text = [regex]::Replace($Text, '(?m)^\s*FFMPEG_PATH\s*=.*$', '')
        $Text = $Text.TrimEnd() + "`r`nFFMPEG_PATH='" + $Ffmpeg.Replace('\', '/') + "'`r`n"
        [IO.File]::WriteAllText($EnvFile, $Text, (New-Object Text.UTF8Encoding($false)))
    }
    if ($NoConfigure) {
        Write-Host "Dependencies installed. Set DISCORD_TOKEN in .env, then run start.bat."
    } else {
        if ($Text -notmatch '(?m)^\s*DISCORD_TOKEN\s*=\s*(?!your_|replace_)\S+') {
            Write-Host "Enter your Discord bot token in .env, save, and close Notepad to continue."
            Start-Process notepad.exe -ArgumentList ('"' + $EnvFile + '"') -Wait
        }
        Invoke-Checked $VenvPython @("main.py", "--check")
        Write-Host "Setup complete. Double-click start.bat to start the tray app."
    }
    if ($InstallStartup) { & (Join-Path $PSScriptRoot "install_startup.ps1") }
    exit 0
} catch {
    Write-Host "Setup failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
